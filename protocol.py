"""Formato del handshake PCM y de la telemetría Android -> PC."""

import struct

MAGIC = b"UA"
VERSION = 2
HEADER_FORMAT = ">2sBIBBH"  # magic, version, rate, channels, bits, chunk_ms
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# Canal de retorno: el móvil informa underruns, búfer adaptativo y backlog.
STATUS_MAGIC = b"US"
STATUS_FLAG_STREAMING = 0x01
STATUS_FORMAT = ">2sBIHHB"  # magic, version, underruns, buffer_ms, backlog_ms, flags
STATUS_SIZE = struct.calcsize(STATUS_FORMAT)


def pack_header(rate, channels, chunk_ms, bits=16):
    """Construye el header del stream (11 bytes)."""
    return struct.pack(
        HEADER_FORMAT, MAGIC, VERSION, int(rate), int(channels), int(bits), int(chunk_ms)
    )


def unpack_header(data):
    """Parsea y valida un header; lanza ValueError si no es compatible."""
    if len(data) != HEADER_SIZE:
        raise ValueError(f"Header de {len(data)} bytes; se esperaban {HEADER_SIZE}")
    magic, version, rate, channels, bits, chunk_ms = struct.unpack(HEADER_FORMAT, data)
    if magic != MAGIC:
        raise ValueError("Magic de protocolo inválido")
    if version != VERSION:
        raise ValueError(f"Versión de protocolo no soportada: {version}")
    return {
        "version": version,
        "rate": rate,
        "channels": channels,
        "bits": bits,
        "chunk_ms": chunk_ms,
    }


def pack_status(underruns, buffer_ms, backlog_ms, flags=STATUS_FLAG_STREAMING):
    """Construye un frame de telemetría del móvil (12 bytes)."""
    return struct.pack(
        STATUS_FORMAT,
        STATUS_MAGIC,
        VERSION,
        max(0, min(0xFFFFFFFF, int(underruns))),
        max(0, min(0xFFFF, int(buffer_ms))),
        max(0, min(0xFFFF, int(backlog_ms))),
        max(0, min(0xFF, int(flags))),
    )


def unpack_status(data):
    """Parsea y valida un frame de telemetría."""
    if len(data) != STATUS_SIZE:
        raise ValueError(
            f"Estado de {len(data)} bytes; se esperaban {STATUS_SIZE}"
        )
    magic, version, underruns, buffer_ms, backlog_ms, flags = struct.unpack(
        STATUS_FORMAT, data
    )
    if magic != STATUS_MAGIC:
        raise ValueError("Magic de estado inválido")
    if version != VERSION:
        raise ValueError(f"Versión de estado no soportada: {version}")
    return {
        "underruns": underruns,
        "buffer_ms": buffer_ms,
        "backlog_ms": backlog_ms,
        "flags": flags,
        "streaming": bool(flags & STATUS_FLAG_STREAMING),
    }


def read_status_frame(sock):
    """Lee un frame completo del canal de retorno; None si el socket se cerró."""
    data = bytearray()
    while len(data) < STATUS_SIZE:
        chunk = sock.recv(STATUS_SIZE - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return unpack_status(bytes(data))
