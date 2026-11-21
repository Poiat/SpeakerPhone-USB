import pytest

from protocol import HEADER_SIZE, MAGIC, VERSION, pack_header, unpack_header


def test_header_roundtrip():
    data = pack_header(48000, 2, 15)
    assert len(data) == HEADER_SIZE == 11
    assert unpack_header(data) == {
        "version": VERSION,
        "rate": 48000,
        "channels": 2,
        "bits": 16,
        "chunk_ms": 15,
    }


def test_invalid_magic():
    data = bytearray(pack_header(48000, 2, 15))
    data[0] = 0
    with pytest.raises(ValueError):
        unpack_header(bytes(data))


def test_invalid_version():
    data = bytearray(pack_header(48000, 2, 15))
    data[2] = VERSION + 1
    with pytest.raises(ValueError):
        unpack_header(bytes(data))


def test_invalid_size():
    with pytest.raises(ValueError):
        unpack_header(b"UA")


def test_magic_constant():
    assert MAGIC == b"UA"
