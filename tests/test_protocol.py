import socket

import pytest

from protocol import (
    HEADER_SIZE,
    MAGIC,
    STATUS_FLAG_STREAMING,
    STATUS_MAGIC,
    STATUS_SIZE,
    VERSION,
    pack_header,
    pack_status,
    read_status_frame,
    unpack_header,
    unpack_status,
)


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
    assert STATUS_MAGIC == b"US"


def test_status_roundtrip():
    data = pack_status(7, 72, 12)
    assert len(data) == STATUS_SIZE == 12
    assert unpack_status(data) == {
        "underruns": 7,
        "buffer_ms": 72,
        "backlog_ms": 12,
        "flags": STATUS_FLAG_STREAMING,
        "streaming": True,
    }


def test_status_values_are_clamped():
    status = unpack_status(pack_status(-5, 70000, -1))
    assert status["underruns"] == 0
    assert status["buffer_ms"] == 65535
    assert status["backlog_ms"] == 0


def test_status_invalid_magic():
    data = bytearray(pack_status(1, 36, 0))
    data[0] = 0
    with pytest.raises(ValueError):
        unpack_status(bytes(data))


def test_status_invalid_version():
    data = bytearray(pack_status(1, 36, 0))
    data[2] = VERSION + 1
    with pytest.raises(ValueError):
        unpack_status(bytes(data))


def test_status_invalid_size():
    with pytest.raises(ValueError):
        unpack_status(b"US")


def test_read_status_frame_handles_partial_reads():
    left, right = socket.socketpair()
    try:
        frame = pack_status(3, 120, 8)
        left.sendall(frame[:5])
        left.sendall(frame[5:])
        assert read_status_frame(right)["buffer_ms"] == 120
    finally:
        left.close()
        right.close()


def test_read_status_frame_returns_none_on_close():
    left, right = socket.socketpair()
    left.close()
    try:
        assert read_status_frame(right) is None
    finally:
        right.close()
