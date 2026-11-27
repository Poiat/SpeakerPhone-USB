import numpy as np
import pytest

from audio_capture import (
    CaptureFormatError,
    downmix_to_stereo,
    pcm_chunk_bytes,
    resolve_capture_format,
)


def pcm_bytes(frames):
    return np.asarray(frames, dtype="<i2").tobytes()


def pcm_frames(data, channels):
    return np.frombuffer(data, dtype="<i2").reshape(-1, channels)


def test_loopback_uses_native_format():
    info = {
        "isLoopbackDevice": True,
        "maxInputChannels": 8,
        "maxOutputChannels": 0,
        "defaultSampleRate": 48000.0,
    }
    fmt = resolve_capture_format(info, 15)
    assert fmt.rate == 48000
    assert fmt.capture_channels == 8
    assert fmt.send_channels == 2
    assert fmt.frames_per_buffer == 720


def test_loopback_uses_output_channels_when_input_missing():
    info = {
        "isLoopbackDevice": True,
        "maxInputChannels": 0,
        "maxOutputChannels": 6,
        "defaultSampleRate": 44100.0,
    }
    fmt = resolve_capture_format(info, 8)
    assert fmt.capture_channels == 6
    assert fmt.send_channels == 2
    assert fmt.frames_per_buffer == 352


def test_loopback_stereo_is_not_downmixed():
    info = {
        "isLoopbackDevice": True,
        "maxInputChannels": 2,
        "defaultSampleRate": 48000,
    }
    fmt = resolve_capture_format(info, 15)
    assert fmt.capture_channels == 2
    assert fmt.send_channels == 2


def test_microphone_is_clamped_to_protocol_channels():
    info = {"maxInputChannels": 8, "defaultSampleRate": 48000}
    fmt = resolve_capture_format(info, 15)
    assert fmt.capture_channels == 2
    assert fmt.send_channels == 2


def test_mono_microphone_stays_mono():
    fmt = resolve_capture_format({"maxInputChannels": 1}, 15)
    assert fmt.capture_channels == 1
    assert fmt.send_channels == 1


def test_missing_channels_raises():
    with pytest.raises(CaptureFormatError):
        resolve_capture_format({"maxInputChannels": 0}, 15)


def test_missing_sample_rate_uses_fallback():
    fmt = resolve_capture_format({"maxInputChannels": 2}, 15, fallback_rate=44100)
    assert fmt.rate == 44100


def test_chunk_size_is_frame_aligned_at_44100():
    # El cálculo directo daba 1411 bytes (impar) con 44.1 kHz y 8 ms; el DSP
    # descartaba un byte por bloque y el stream quedaba desalineado.
    assert pcm_chunk_bytes(44100, 2, 8) == 1408
    assert pcm_chunk_bytes(44100, 2, 15) == 2644
    assert pcm_chunk_bytes(44100, 1, 15) == 1322
    assert pcm_chunk_bytes(48000, 2, 15) == 2880


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("chunk_ms", [8, 15, 30])
def test_chunk_size_always_aligns_to_frames(rate, channels, chunk_ms):
    size = pcm_chunk_bytes(rate, channels, chunk_ms)
    assert size % (channels * 2) == 0
    assert size > 0


def test_chunk_size_never_returns_zero():
    assert pcm_chunk_bytes(1, 1, 1) == 2


def test_stereo_passthrough_is_exact():
    data = pcm_bytes([1, 2, 3, 4])
    assert downmix_to_stereo(data, 2) == data
    assert downmix_to_stereo(data, 1) == data


def test_downmix_keeps_front_left_and_right():
    frames = [[1000, 2000, 0, 0, 0, 0]]
    out = downmix_to_stereo(pcm_bytes(frames), 6)
    assert pcm_frames(out, 2).tolist() == [[1000, 2000]]


def test_downmix_adds_center_to_both_channels():
    frames = [[0, 0, 1000, 0, 0, 0]]
    out = downmix_to_stereo(pcm_bytes(frames), 6)
    assert pcm_frames(out, 2).tolist() == [[707, 707]]


def test_downmix_adds_surrounds_to_matching_side():
    frames = [[0, 0, 0, 0, 1000, 2000]]
    out = downmix_to_stereo(pcm_bytes(frames), 6)
    assert pcm_frames(out, 2).tolist() == [[707, 1414]]


def test_downmix_attenuates_lfe():
    frames = [[0, 0, 0, 1000, 0, 0]]
    out = downmix_to_stereo(pcm_bytes(frames), 6)
    assert pcm_frames(out, 2).tolist() == [[500, 500]]


def test_downmix_51_produces_two_channels_per_frame():
    frames = [[100, 200, 300, 400, 500, 600]] * 3
    out = downmix_to_stereo(pcm_bytes(frames), 6)
    assert len(pcm_frames(out, 2)) == 3


def test_downmix_truncates_incomplete_frames():
    data = pcm_bytes([1, 2, 3, 4, 5, 6]) + b"\x00\x00"
    out = downmix_to_stereo(data, 6)
    assert len(out) == 4


def test_downmix_clips_instead_of_wrapping():
    frames = [[32767] * 6]
    out = downmix_to_stereo(pcm_bytes(frames), 6)
    assert pcm_frames(out, 2).tolist() == [[32767, 32767]]
