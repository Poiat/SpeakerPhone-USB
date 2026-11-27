"""Ajuste automático del búfer del PC a partir de la telemetría Android."""

BUFFER_STEPS_MS = (8, 15, 30, 60)
# El búfer adaptativo del móvil llega como máximo a 120 ms; a partir de 96 ms
# se considera que ya no puede absorber más y que el PC debe ceder margen.
AUTO_TUNE_MIN_ANDROID_BUFFER_MS = 96
AUTO_TUNE_MIN_STREAK = 3
AUTO_TUNE_COOLDOWN_S = 30.0


def next_buffer_step(current_ms):
    """Siguiente perfil de búfer por encima del actual, o None si ya es máximo."""
    try:
        current = int(current_ms)
    except (TypeError, ValueError):
        return None
    for step in BUFFER_STEPS_MS:
        if step > current:
            return step
    return None


def auto_tune_target(
    current_ms,
    android_buffer_ms,
    underrun_delta,
    streak,
    seconds_since_last_tune,
):
    """
    Decide si el PC debe subir su búfer.

    Solo se sube cuando el móvil lleva varios intervalos con underruns nuevos,
    su búfer adaptativo está cerca del máximo y pasó el tiempo de espera.
    """
    if underrun_delta <= 0:
        return None
    if int(android_buffer_ms) < AUTO_TUNE_MIN_ANDROID_BUFFER_MS:
        return None
    if int(streak) < AUTO_TUNE_MIN_STREAK:
        return None
    if float(seconds_since_last_tune) < AUTO_TUNE_COOLDOWN_S:
        return None
    return next_buffer_step(current_ms)


def format_stats(pc_ms, android_ms, backlog_ms, underruns, auto=False):
    """Texto compacto para la tarjeta de estado del PC."""
    pc = f"{int(pc_ms)} ms" + (" (auto)" if auto else "")
    return (
        f"PC {pc} · Móvil {int(android_ms)} ms · "
        f"Backlog {int(backlog_ms)} ms · Underruns {int(underruns)}"
    )
