"""Decrypt an EZVIZ cloud stream while it is still arriving.

The EZVIZ app plays an encrypted camera live, so live decryption is clearly
possible - and the encryption is not what stands in the way. Only the first
4 KB of each video NAL is encrypted, with AES-ECB, and NALs are small: this is
a transform that can run on a stream, not something that needs the whole file.

What needs the whole file is ``pyezvizapi``'s *interface*. Its decrypt path
collects everything first, decrypts it in one pass and only then remuxes, which
is why an encrypted camera came out as a clip rather than a live view: a wait,
a few seconds of video, then the end of the stream.

So the library's own decryption is used - unchanged, one call per piece - on
pieces that are safe to hand it. A piece is safe when it starts at a NAL
boundary and ends before the next one, because the decrypter carries state
across a NAL body and has none to carry in or out at a boundary. Everything
after the last boundary is held back until the rest of it arrives.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
import logging
import subprocess
import threading
from typing import Any, BinaryIO

from .vendor.pyezvizapi.cloud_stream import (
    _open_cloud_mpegts_remux_process,
    _write_cloud_stream_payloads,
    open_cloud_stream,
)
from .vendor.pyezvizapi.stream import (
    _find_nal_start_codes,
    _is_video_pes_stream_id,
    _mpeg_ps_complete_packet_ranges,
    _pes_payload_start,
    decrypt_hikvision_ps_video,
    detect_hikvision_ps_video_nalu_header_size,
)

_LOGGER = logging.getLogger(__name__)

# How much may be held back waiting for a NAL boundary before giving up on
# finding one. A second of video is far more than the gap between two NALs, so
# reaching this means the stream is not what we think it is; the buffer is
# decrypted as it stands rather than growing without end.
MAX_HELD_BYTES = 4 * 1024 * 1024


class NalAlignedDecryptor:
    """A file-like object that decrypts what is written through it."""

    def __init__(self, sink: BinaryIO, key: str | bytes) -> None:
        """Decrypt into ``sink`` with the camera's key."""
        self._sink = sink
        self._key = key
        self._held = bytearray()
        self._header_size: int | None = None

    def write(self, payload: bytes) -> int:
        """Take stream bytes, and pass on whatever is complete."""
        self._held += payload
        cut = self._boundary()
        if cut <= 0 and len(self._held) > MAX_HELD_BYTES:
            _LOGGER.debug("No NAL boundary in %d bytes; decrypting anyway", len(self._held))
            cut = len(self._held)
        if cut > 0:
            self._emit(cut)
        return len(payload)

    def flush(self) -> None:
        """Flush the sink; held bytes are not complete yet."""
        self._sink.flush()

    def close(self) -> None:
        """Decrypt the last piece, which no boundary will ever follow."""
        if self._held:
            self._emit(len(self._held))
        self._sink.flush()

    # ------------------------------------------------------------------

    def _emit(self, cut: int) -> None:
        """Decrypt the first ``cut`` bytes and write them on."""
        piece = bytes(self._held[:cut])
        del self._held[:cut]

        if self._header_size is None:
            self._header_size = detect_hikvision_ps_video_nalu_header_size(
                piece, self._key
            )

        self._sink.write(
            decrypt_hikvision_ps_video(
                piece, self._key, nalu_header_size=self._header_size
            )
        )
        self._sink.flush()

    def _boundary(self) -> int:
        """Return where the last complete NAL ends, or 0 if none does.

        A video packet whose payload opens with a NAL start code begins a NAL,
        so everything before it is whole. The last such packet in what is held
        is the furthest that can safely be decrypted.
        """
        data = bytes(self._held)
        boundary = 0
        for packet in _mpeg_ps_complete_packet_ranges(data):
            if not _is_video_pes_stream_id(packet.stream_id):
                continue
            payload_start = _pes_payload_start(data, packet.start)
            if payload_start is None or payload_start >= packet.end:
                continue
            starts = _find_nal_start_codes(data, payload_start, packet.end)
            if starts and starts[0][0] == payload_start:
                boundary = packet.start
        return boundary


def copy_decrypted_cloud_stream(
    client: Any,
    serial: str,
    output: BinaryIO,
    key: str | bytes,
    *,
    ffmpeg_path: str = "ffmpeg",
    open_stream: Callable[..., Any] = open_cloud_stream,
) -> None:
    """Stream one encrypted camera as MPEG-TS, decrypting as it goes.

    The shape is the library's own: a thread feeding FFmpeg's standard input
    while this one copies its output. The only thing between the two is the
    decrypter.

    Raises:
        PyEzvizError: If the stream cannot be opened or FFmpeg fails.
    """
    process = _open_cloud_mpegts_remux_process(ffmpeg_path)
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        raise OSError("Could not open FFmpeg pipes")

    failures: list[Exception] = []

    def _feed() -> None:
        decryptor = NalAlignedDecryptor(stdin, key)
        try:
            with open_stream(client, serial) as stream:
                stream.start()
                _write_cloud_stream_payloads(
                    stream,
                    decryptor,
                    max_packets=None,
                    duration_seconds=None,
                    flush_each=True,
                )
            decryptor.close()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as err:  # noqa: BLE001 - handed to the caller below
            failures.append(err)
        finally:
            with suppress(OSError):
                stdin.close()

    feeder = threading.Thread(target=_feed, name="ezviz-decrypt", daemon=True)
    feeder.start()
    try:
        while chunk := stdout.read(65536):
            output.write(chunk)
            output.flush()
    finally:
        if process.poll() is None:
            process.terminate()
        feeder.join(timeout=2)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    if failures:
        raise failures[0]
