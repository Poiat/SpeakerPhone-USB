"""Resolución del formato de captura y mezcla de canales para PCM s16le."""

from dataclasses import dataclass

import numpy as np

# El receptor Android solo acepta 1 o 2 canales (ver AudioStreamService.kt).
MAX_SEND_CHANNELS = 2
MIN_FRAMES_PER_BUFFER = 128

# Orden de canales WAVE típico (FL, FR, FC, LFE, ...). Coeficientes de
# downmix estándar: los canales frontales se suman directos, el central y los
# surrounds a -3 dB y el LFE a -6 dB.
_FL = 1.0
_CTR = 0.70710678
_LFE = 0.5

_DOWNMIX_MATRIX = {
    # FL FR FC
    3: ((_FL, 0.0, _CTR), (0.0, _FL, _CTR)),
    # FL FR BL BR
    4: ((_FL, 0.0, _CTR, 0.0), (0.0, _FL, 0.0, _CTR)),
    # FL FR FC BL BR
    5: ((_FL, 0.0, _CTR, _CTR, 0.0), (0.0, _FL, _CTR, 0.0, _CTR)),
    # FL FR FC LFE BL BR
    6: (
        (_FL, 0.0, _CTR, _LFE, _CTR, 0.0),
        (0.0, _FL, _CTR, _LFE, 0.0, _CTR),
    ),
    # FL FR FC LFE BC SL SR
    7: (
        (_FL, 0.0, _CTR, _LFE, _CTR, _CTR, 0.0),
        (0.0, _FL, _CTR, _LFE, _CTR, 0.0, _CTR),
    ),
    # FL FR FC LFE BL BR SL SR
    8: (
        (_FL, 0.0, _CTR, _LFE, _CTR, 0.0, _CTR, 0.0),
        (0.0, _FL, _CTR, _LFE, 0.0, _CTR, 0.0, _CTR),
    ),
}


class CaptureFormatError(ValueError):
    """El dispositivo no reporta un formato de captura utilizable."""


@dataclass(frozen=True)
class CaptureFormat:
    rate: int
    capture_channels: int
    send_channels: int
    frames_per_buffer: int


def _positive_int(value) -> int:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def resolve_capture_format(dev_info, chunk_ms, fallback_rate=48000) -> CaptureFormat:
    """
    Calcula cómo abrir el dispositivo y cómo enviarlo a Android.

    WASAPI en modo compartido exige que la frecuencia y el número de canales
    coincidan con el formato de mezcla del endpoint. Por eso los dispositivos
    [Loopback] se abren con sus canales y frecuencia nativos
    (maxInputChannels / defaultSampleRate); el protocolo Android solo admite
    mono o estéreo, así que los canales extra se mezclan después.
    """
    is_loopback = bool(dev_info.get("isLoopbackDevice", False))

    rate = _positive_int(dev_info.get("defaultSampleRate"))
    if rate <= 0:
        rate = max(1, int(fallback_rate))

    native_channels = _positive_int(dev_info.get("maxInputChannels"))
    if native_channels <= 0 and is_loopback:
        # Algunos drivers solo exponen el número de canales del render.
        native_channels = _positive_int(dev_info.get("maxOutputChannels"))
    if native_channels <= 0:
        raise CaptureFormatError(
            "El dispositivo no reporta canales de entrada utilizables"
        )

    if is_loopback:
        capture_channels = native_channels
    else:
        # Los micrófonos se limitan al formato del protocolo (1-2 canales).
        capture_channels = min(native_channels, MAX_SEND_CHANNELS)
    send_channels = min(capture_channels, MAX_SEND_CHANNELS)

    frames = max(
        MIN_FRAMES_PER_BUFFER,
        int(rate * max(1, int(chunk_ms)) / 1000),
    )
    return CaptureFormat(rate, capture_channels, send_channels, frames)


def _downmix_matrix(channels: int):
    matrix = _DOWNMIX_MATRIX.get(channels)
    if matrix is not None:
        return matrix
    # Diseño genérico: L/R directos y el resto a -3 dB; el cuarto canal se
    # asume LFE en las disposiciones surround habituales.
    left = (_FL, 0.0) + (_CTR,) * (channels - 2)
    right = (0.0, _FL) + (_CTR,) * (channels - 2)
    if channels > 3:
        left = left[:3] + (_LFE,) + left[4:]
        right = right[:3] + (_LFE,) + right[4:]
    return (left, right)


def downmix_to_stereo(data: bytes, channels: int) -> bytes:
    """
    Convierte PCM s16le de ``channels`` canales a estéreo.

    Solo se usa cuando la captura nativa supera los 2 canales (por ejemplo un
    loopback WASAPI 5.1/7.1). Con 1 o 2 canales devuelve los datos intactos.
    """
    if channels <= 2:
        return data

    frame_bytes = channels * 2
    usable = len(data) - (len(data) % frame_bytes)
    if usable <= 0:
        return b""

    frames = np.frombuffer(data[:usable], dtype="<i2").reshape(-1, channels)
    matrix = np.asarray(_downmix_matrix(channels), dtype=np.float32)
    mixed = frames.astype(np.float32) @ matrix.T
    out = np.rint(np.clip(mixed, -32768.0, 32767.0)).astype("<i2")
    return out.tobytes()
