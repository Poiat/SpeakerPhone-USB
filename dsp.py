"""Procesamiento PCM compartido (ganancia, limitador suave y medidor de nivel)."""

import math

import numpy as np

CLIP_THRESHOLD = 0.98


def process_pcm_s16le(data, gain_linear, soft_limiter, compute_meter):
    """
    Aplica ganancia y limitador suave al bloque PCM s16le.

    Devuelve (bytes_procesados, nivel_0_1, hay_saturacion). El nivel es RMS
    normalizado y solo se calcula cuando compute_meter es True.
    """
    usable = len(data) - (len(data) & 1)
    if usable <= 0:
        return data, 0.0, False

    need_dsp = (abs(gain_linear - 1.0) > 0.005) or soft_limiter
    if not need_dsp and not compute_meter:
        return data, 0.0, False

    raw = np.frombuffer(data[:usable], dtype="<i2")
    clipping = False

    if need_dsp:
        scaled = raw.astype(np.float32) * np.float32(gain_linear)
        if soft_limiter:
            # Knee suave por encima de 0.75 para evitar distorsión.
            normalized = scaled / np.float32(32768.0)
            absolute = np.abs(normalized)
            mask = absolute > 0.75
            if np.any(mask):
                excess = (absolute[mask] - 0.75) / 0.25
                limited = 0.75 + 0.245 * np.tanh(excess)
                normalized[mask] = np.copysign(limited, normalized[mask])
            clipping = bool(np.any(np.abs(normalized) >= 0.99))
            out_samples = np.clip(
                normalized * np.float32(32767.0), -32768, 32767
            ).astype("<i2")
        else:
            out_samples = np.clip(scaled, -32768, 32767).astype("<i2")
            clipping = bool(
                np.any(scaled > 32767.0) or np.any(scaled < -32768.0)
            )
        out_bytes = out_samples.tobytes()
        meter_source = out_samples
    else:
        out_bytes = data[:usable]
        meter_source = raw

    if not compute_meter:
        return out_bytes, 0.0, clipping

    sub = meter_source[::4] if meter_source.size > 128 else meter_source
    if sub.size == 0:
        return out_bytes, 0.0, clipping

    sub_float = sub.astype(np.float32)
    peak = float(np.max(np.abs(sub_float))) / 32768.0
    rms = float(np.sqrt(np.mean(sub_float * sub_float))) / 32768.0
    level = min(1.0, math.pow(rms * 2.5, 0.65))
    if peak >= CLIP_THRESHOLD and not soft_limiter:
        clipping = True
    return out_bytes, level, clipping
