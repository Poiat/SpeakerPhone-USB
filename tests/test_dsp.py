import numpy as np

from dsp import CLIP_THRESHOLD, process_pcm_s16le


def make_pcm(values):
    return np.asarray(values, dtype="<i2").tobytes()


def test_silence_has_no_level():
    out, level, clipping = process_pcm_s16le(make_pcm([0] * 512), 1.0, False, True)
    assert out == make_pcm([0] * 512)
    assert level == 0.0
    assert clipping is False


def test_gain_clips_without_limiter():
    out, level, clipping = process_pcm_s16le(make_pcm([20000] * 64), 2.0, False, True)
    samples = np.frombuffer(out, dtype="<i2")
    assert samples.max() == 32767
    assert clipping is True
    assert 0.0 <= level <= 1.0


def test_limiter_reduces_peak():
    pcm = make_pcm([32767, -32768] * 64)
    out, _level, _clipping = process_pcm_s16le(pcm, 1.0, True, False)
    peak = float(np.max(np.abs(np.frombuffer(out, dtype="<i2")))) / 32768.0
    assert peak < 1.0


def test_clip_threshold_constant():
    assert 0.0 < CLIP_THRESHOLD < 1.0


def test_level_is_normalized():
    _out, level, _clipping = process_pcm_s16le(
        make_pcm([int(32767 * 0.5)] * 512), 1.0, False, True
    )
    assert 0.0 < level <= 1.0
