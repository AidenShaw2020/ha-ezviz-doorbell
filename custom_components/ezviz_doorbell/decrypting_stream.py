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
pieces that are safe to hand it. A piece has to begin where a packet does, or
its parser will not recognise it, and it must not end inside a NAL's encrypted
prefix, because the decrypter carries that state across the bytes of one NAL
and has none to carry from one call to the next. Everything past the last such
point is held back until the rest of it arrives.

The one limit left: a run of frames each smaller than the 4 KB prefix offers
nowhere to cut, and waits for the next larger frame. Cameras send frames larger
than that most of the time, so in practice video comes out continuously.
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
    HIKVISION_NAL_ENCRYPTED_PREFIX_LENGTH,
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
            cut = self._boundary(allow_damage=True)
            _LOGGER.debug(
                "No safe cut in %d bytes; taking one at %d", len(self._held), cut
            )
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

    def _boundary(self, *, allow_damage: bool = False) -> int:
        """Return how much can be decrypted now, or 0 if nothing can.

        A piece has to start where a packet does, or the decrypter's parser
        will not recognise it and will hand it back untouched. So the cut is
        always a packet boundary - but not any packet boundary: it must not
        fall inside a NAL's encrypted prefix, because the decrypter carries the
        state for that across the bytes of one NAL and has none to carry from
        one call to the next. Anything after such a cut would stay encrypted.

        The newest NAL is never cut into at all: how long it is, and therefore
        how much of it is encrypted, is not yet known.

        A NAL rarely begins exactly where a packet's payload does; it lands
        somewhere inside one. Waiting for the two to coincide - which is what
        this used to do - held video back until whichever packet happened to
        line up, and played it out in bursts with long stalls between them.
        """
        data = bytes(self._held)
        packets = _mpeg_ps_complete_packet_ranges(data)
        if not packets:
            return 0

        starts, cuts = self._survey(data, packets)
        if not starts:
            # Nothing here begins a NAL, so nothing here is mid-NAL either.
            return packets[-1].end

        newest = starts[-1][0]
        boundary = 0
        for offset, video_offset in cuts:
            if offset > newest and not allow_damage:
                # Out of room is the one reason to cut into the newest NAL:
                # how far it is encrypted is not yet known, so this spoils one
                # frame - which beats a buffer with no end to it.
                break
            if any(
                start < offset and video_start < video_offset < video_end
                for start, video_start, video_end in starts
            ):
                continue
            boundary = offset
        return boundary

    def _survey(
        self, data: bytes, packets: list[Any]
    ) -> tuple[list[tuple[int, int, int]], list[tuple[int, int]]]:
        """Return where NALs start and where a piece could end.

        Both in two coordinates at once: the offset in the buffer, which is
        where a cut is actually made, and the offset counting video payload
        only, which is what the 4 KB encrypted prefix is measured in. They are
        not the same - packet headers sit between the payload bytes - and
        measuring the prefix in buffer bytes cuts it short by one header per
        packet it spans, which leaves the tail of it encrypted.
        """
        starts: list[tuple[int, int, int]] = []
        cuts: list[tuple[int, int]] = []
        video_offset = 0

        for packet in packets:
            payload_start = None
            if _is_video_pes_stream_id(packet.stream_id):
                payload_start = _pes_payload_start(data, packet.start)

            if payload_start is None or payload_start >= packet.end:
                cuts.append((packet.end, video_offset))
                continue

            for position, _ in _find_nal_start_codes(data, payload_start, packet.end):
                at = video_offset + (position - payload_start)
                starts.append(
                    (position, at, at + HIKVISION_NAL_ENCRYPTED_PREFIX_LENGTH + 6)
                )

            video_offset += packet.end - payload_start
            cuts.append((packet.end, video_offset))

        # A NAL's encrypted part stops where the NAL does, however short that
        # makes it, so the next NAL's start is also the end of this one's.
        capped = [
            (offset, at, min(end, starts[index + 1][1]))
            if index + 1 < len(starts)
            else (offset, at, end)
            for index, (offset, at, end) in enumerate(starts)
        ]
        return capped, cuts


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
