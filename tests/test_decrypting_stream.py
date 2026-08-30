"""Does decrypting a stream in pieces give the same answer as in one go?

There is no encrypted EZVIZ camera to test against here, so the test builds a
stream the way a camera would: MPEG-PS packets carrying H.264 NALs whose first
4 KB are AES-ECB encrypted. What is checked is equivalence - the library
decrypts the whole thing in one pass, this decrypts it in arbitrary pieces as
it arrives, and the two must agree byte for byte. If they do, live decryption
is the same operation as the library's, only sooner.
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


@pytest.fixture
def stream() -> tuple[bytes, bytes]:
    """Return one encrypted stream and what it should decrypt to."""
    plain_parts: list[bytes] = []
    encrypted_parts: list[bytes] = []
    # Sizes either side of the 4 KB encrypted prefix, so both a NAL that is
    # encrypted end to end and one with a clear tail are covered.
    for index, size in enumerate((6000, 1200, 9000, 3000)):
        nal = _nal(bytes((index + 7) % 251 for _ in range(size)))
        plain_parts.append(_pes(nal))
        encrypted_parts.append(_pes(_encrypt_prefix(nal)))
    return b"".join(encrypted_parts), b"".join(plain_parts)


def test_the_test_data_is_what_the_library_expects(
    stream: tuple[bytes, bytes],
) -> None:
    """The library must decrypt it back, or the test proves nothing."""
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
