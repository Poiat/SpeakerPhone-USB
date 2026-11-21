"""Formato del handshake PCM entre las apps de PC y Android."""

import struct

MAGIC = b"UA"
VERSION = 1
HEADER_FORMAT = ">2sBIBBH"  # magic, version, rate, channels, bits, chunk_ms
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


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
