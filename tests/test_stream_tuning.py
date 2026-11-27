from stream_tuning import (
    auto_tune_target,
    format_stats,
    next_buffer_step,
)


def test_next_buffer_step_walks_profiles():
    assert next_buffer_step(8) == 15
    assert next_buffer_step(15) == 30
    assert next_buffer_step(30) == 60
    assert next_buffer_step(59) == 60
    assert next_buffer_step(60) is None
    assert next_buffer_step("15") == 30
    assert next_buffer_step(None) is None


def test_auto_tune_requires_phone_buffer_near_max():
    assert (
        auto_tune_target(
            current_ms=15,
            android_buffer_ms=72,
            underrun_delta=5,
            streak=5,
            seconds_since_last_tune=60,
        )
        is None
    )


def test_auto_tune_requires_persistent_underruns():
    assert (
        auto_tune_target(
            current_ms=15,
            android_buffer_ms=120,
            underrun_delta=5,
            streak=2,
            seconds_since_last_tune=60,
        )
        is None
    )
    assert (
        auto_tune_target(
            current_ms=15,
            android_buffer_ms=120,
            underrun_delta=0,
            streak=5,
            seconds_since_last_tune=60,
        )
        is None
    )


def test_auto_tune_respects_cooldown_and_maximum():
    assert (
        auto_tune_target(
            current_ms=15,
            android_buffer_ms=120,
            underrun_delta=5,
            streak=3,
            seconds_since_last_tune=10,
        )
        is None
    )
    assert (
        auto_tune_target(
            current_ms=60,
            android_buffer_ms=120,
            underrun_delta=5,
            streak=3,
            seconds_since_last_tune=60,
        )
        is None
    )


def test_auto_tune_raises_one_step():
    assert (
        auto_tune_target(
            current_ms=15,
            android_buffer_ms=120,
            underrun_delta=5,
            streak=3,
            seconds_since_last_tune=60,
        )
        == 30
    )


def test_format_stats_marks_auto_buffer():
    text = format_stats(30, 120, 8, 4, auto=True)
    assert "PC 30 ms (auto)" in text
    assert "Móvil 120 ms" in text
    assert "Backlog 8 ms" in text
    assert "Underruns 4" in text
    assert "(auto)" not in format_stats(15, 72, 0, 0)
