"""Does decrypting a stream in pieces give the same answer as in one go?

There is no encrypted EZVIZ camera to test against here, so the tests build one
the way a camera would: NALs whose first 4 KB are AES-ECB encrypted, packed
into MPEG-PS. What is checked is equivalence - the library decrypts the whole
thing in one pass, this decrypts it in pieces as it arrives, and the two must
agree byte for byte - and that the pieces keep coming rather than arriving in
bursts, which is the difference between video and a slideshow.

Two packings, because they are not the same problem. One NAL per packet is the
easy case. Packets of a fixed size, with NALs starting wherever they fall, is
what a camera actually sends.
"""

from __future__ import annotations

from io import BytesIO

from Crypto.Cipher import AES
import pytest

from custom_components.ezviz_doorbell.decrypting_stream import NalAlignedDecryptor
from custom_components.ezviz_doorbell.vendor.pyezvizapi.stream import (
    HIKVISION_NAL_ENCRYPTED_PREFIX_LENGTH,
    decrypt_hikvision_ps_video,
)

KEY = "ABCDEF"
NAL_HEADER = b"\x65"  # H.264 IDR slice
START_CODE = b"\x00\x00\x00\x01"
# Frame sizes as a camera produces them: mostly larger than the 4 KB that
# gets encrypted, with a small one among them.
NAL_SIZES = (6000, 9000, 1200, 5000, 7000, 8000)


def _pes(payload: bytes) -> bytes:
    """Wrap a payload in a video PES packet."""
    # Flags marking an MPEG-2 PES header, then a zero length header.
    body = b"\x80\x00\x00" + payload
    return b"\x00\x00\x01\xe0" + len(body).to_bytes(2, "big") + body


def _nal(body: bytes) -> bytes:
    """Return one Annex B NAL."""
    return START_CODE + NAL_HEADER + body


def _encrypt_prefix(nal: bytes) -> bytes:
    """Encrypt a NAL the way an EZVIZ camera does.

    Only the first 4 KB of the body is encrypted, in AES-ECB, with the key
    padded out to sixteen bytes - and only whole blocks of it.
    """
    body_start = len(START_CODE) + len(NAL_HEADER)
    body = bytearray(nal[body_start:])
    length = min(len(body), HIKVISION_NAL_ENCRYPTED_PREFIX_LENGTH)
    length -= length % AES.block_size
    cipher = AES.new(KEY.encode().ljust(16, b"\0")[:16], AES.MODE_ECB)
    body[:length] = cipher.encrypt(bytes(body[:length]))
    return nal[:body_start] + bytes(body)


def _nals() -> tuple[bytes, bytes]:
    """Return the encrypted NAL stream and what it should decrypt to."""
    plain: list[bytes] = []
    encrypted: list[bytes] = []
    for index, size in enumerate(NAL_SIZES):
        nal = _nal(bytes((index + 7) % 251 for _ in range(size)))
        plain.append(nal)
        encrypted.append(_encrypt_prefix(nal))
    return b"".join(encrypted), b"".join(plain)


def _one_nal_per_packet() -> tuple[bytes, bytes]:
    """Pack each NAL into its own packet - the easy case."""
    plain: list[bytes] = []
    encrypted: list[bytes] = []
    for index, size in enumerate(NAL_SIZES):
        nal = _nal(bytes((index + 7) % 251 for _ in range(size)))
        plain.append(_pes(nal))
        encrypted.append(_pes(_encrypt_prefix(nal)))
    return b"".join(encrypted), b"".join(plain)


def _fixed_size_packets(size: int = 2048) -> tuple[bytes, bytes]:
    """Pack the NAL stream into packets of a fixed size.

    Which is what a camera does, and it means a NAL starts wherever it happens
    to fall - hardly ever at the beginning of a packet's payload.
    """
    encrypted_nals, plain_nals = _nals()

    def pack(data: bytes) -> bytes:
        return b"".join(
            _pes(data[start : start + size]) for start in range(0, len(data), size)
        )

    return pack(encrypted_nals), pack(plain_nals)


PACKINGS = {
    "one NAL per packet": _one_nal_per_packet,
    "fixed size packets": _fixed_size_packets,
}


@pytest.fixture(params=list(PACKINGS), ids=list(PACKINGS))
def stream(request: pytest.FixtureRequest) -> tuple[bytes, bytes]:
    """Return one encrypted stream and what it should decrypt to."""
    return PACKINGS[request.param]()


def test_the_test_data_is_what_the_library_expects(
    stream: tuple[bytes, bytes],
) -> None:
    """The library must decrypt it back, or the tests prove nothing."""
    encrypted, plain = stream
    assert encrypted != plain
    assert decrypt_hikvision_ps_video(encrypted, KEY, nalu_header_size=1) == plain


@pytest.mark.parametrize("chunk", [1, 7, 137, 1024, 5000, 100000])
def test_decrypting_as_it_arrives_matches_decrypting_it_whole(
    stream: tuple[bytes, bytes], chunk: int
) -> None:
    """However the stream is cut up, the result is the same."""
    encrypted, plain = stream

    sink = BytesIO()
    decryptor = NalAlignedDecryptor(sink, KEY)
    for start in range(0, len(encrypted), chunk):
        decryptor.write(encrypted[start : start + chunk])
    decryptor.close()

    assert sink.getvalue() == plain


def test_it_hands_video_over_before_the_stream_ends(
    stream: tuple[bytes, bytes],
) -> None:
    """The whole point: video comes out while more is still coming in."""
    encrypted, plain = stream

    sink = BytesIO()
    decryptor = NalAlignedDecryptor(sink, KEY)
    decryptor.write(encrypted)

    # Everything but the last NAL, which no boundary has followed yet.
    assert 0 < len(sink.getvalue()) < len(plain)
    decryptor.close()
    assert sink.getvalue() == plain


def test_video_keeps_coming_rather_than_arriving_in_bursts(
    stream: tuple[bytes, bytes],
) -> None:
    """Video should be handed on as the frames end, not saved up.

    Waiting for a NAL to begin exactly where a packet's payload does - which
    hardly ever happens - held it back until whichever packet happened to line
    up, and played it out in bursts with long stalls between them.

    What can be handed over is everything up to a point no encrypted prefix
    straddles, so a frame larger than that 4 KB prefix always offers one. A run
    of frames smaller than it has to wait for the next big one, which is the
    one limit left in this.
    """
    encrypted, _ = stream

    sink = BytesIO()
    decryptor = NalAlignedDecryptor(sink, KEY)

    sizes = []
    for start in range(0, len(encrypted), 512):
        decryptor.write(encrypted[start : start + 512])
        sizes.append(len(sink.getvalue()))

    # Six frames go in, five of them bigger than the encrypted prefix, so video
    # should come out several times along the way rather than once at the end.
    steps = len({size for size in sizes if size})
    assert steps >= 3, f"video came out in {steps} step(s): {sizes}"
