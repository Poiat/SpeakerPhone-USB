import io

import pytest

import speakerphone_linux
from speakerphone_linux import (
    LogRepeatCounter,
    StreamController,
    build_audio_sources,
    format_parec_error,
    is_monitor_source,
    parse_pactl_sinks,
)

PACTL_SINKS = """
Sink #32
\tState: SUSPENDED
\tName: audiorelay-virtual-mic-sink
\tDescription: AudioRelay Mic&Sink
\tMonitor Source: audiorelay-virtual-mic-sink
\tProperties:
\t\tdevice.description = "AudioRelay Mic&Sink"

Sink #38
\tState: RUNNING
\tName: alsa_output.usb-G432.analog-stereo
\tDescription: Logitech G432 Analog Stereo
\tMonitor Source: alsa_output.usb-G432.analog-stereo.monitor

Sink #40
\tState: IDLE
\tName: alsa_output.pci-hdmi.stereo
\tDescription: HDMI Audio
\tMonitor Source: alsa_output.pci-hdmi.stereo.monitor

Sink #41
\tState: IDLE
\tName: usb_phone
\tDescription: Telefono-USB
\tMonitor Source: usb_phone.monitor
"""


def test_parse_pactl_sinks():
    sinks = parse_pactl_sinks(PACTL_SINKS)
    assert [sink["name"] for sink in sinks] == [
        "audiorelay-virtual-mic-sink",
        "alsa_output.usb-G432.analog-stereo",
        "alsa_output.pci-hdmi.stereo",
        "usb_phone",
    ]
    assert sinks[0]["description"] == "AudioRelay Mic&Sink"
    assert sinks[0]["monitor"] == "audiorelay-virtual-mic-sink"


def test_build_audio_sources_prioritizes_outputs():
    sinks = parse_pactl_sinks(PACTL_SINKS)
    options = build_audio_sources(sinks, "alsa_output.usb-G432.analog-stereo")

    ids = [source_id for source_id, _label in options]
    labels = dict(options)
    assert ids[0] == "__default__"
    assert ids[1] == "__virtual__"
    assert ids[2] == "alsa_output.usb-G432.analog-stereo.monitor"
    assert ids[3] == "audiorelay-virtual-mic-sink"
    assert ids[4] == "alsa_output.pci-hdmi.stereo.monitor"
    assert "usb_phone.monitor" not in ids

    assert "Logitech G432 Analog Stereo" in labels["__default__"]
    assert "alsa_output.usb-G432.analog-stereo.monitor" in labels["__default__"]
    assert "usb_phone" in labels["__virtual__"]
    assert "silencia" in labels["__virtual__"]
    assert labels[ids[2]].startswith("[Predeterminada]")
    assert labels[ids[3]].startswith("[Monitor]")
    assert "AudioRelay Mic&Sink" in labels[ids[3]]


def test_build_audio_sources_fallback_excludes_inputs():
    options = build_audio_sources(
        [],
        "",
        [
            "alsa_output.usb-G432.analog-stereo.monitor",
            "alsa_input.usb-G432.mono-fallback",
            "usb_phone.monitor",
        ],
    )
    ids = [source_id for source_id, _label in options]
    assert ids == [
        "__default__",
        "__virtual__",
        "alsa_output.usb-G432.analog-stereo.monitor",
    ]
    assert options[2][1].startswith("[Monitor]")


def test_is_monitor_source_accepts_monitors_and_rejects_inputs(monkeypatch):
    monkeypatch.setattr(
        speakerphone_linux, "run_cmd", lambda *_args, **_kwargs: PACTL_SINKS
    )
    assert is_monitor_source("@DEFAULT_MONITOR@")
    assert is_monitor_source("alsa_output.usb-G432.analog-stereo.monitor")
    assert is_monitor_source("audiorelay-virtual-mic-sink")
    assert not is_monitor_source("alsa_input.usb-G432.mono-fallback")
    assert not is_monitor_source("")


def test_is_monitor_source_fallback_requires_monitor_suffix(monkeypatch):
    def fake_run_cmd(cmd, timeout=None):
        if "short" in cmd:
            return (
                "67\talsa_output.x.monitor\tPipeWire\ts16le 2ch 48000Hz\n"
                "68\talsa_input.mic\tPipeWire\ts16le 1ch 48000Hz"
            )
        raise RuntimeError("servidor sin sinks")

    monkeypatch.setattr(speakerphone_linux, "run_cmd", fake_run_cmd)
    assert is_monitor_source("alsa_output.x.monitor")
    assert not is_monitor_source("alsa_input.mic")


def test_default_source_uses_default_monitor(monkeypatch):
    controller = _controller(monkeypatch)
    assert controller._resolve_source() == "@DEFAULT_MONITOR@"


def test_format_parec_error_includes_exit_code_and_stderr():
    stderr = "Connection failure: Connection refused\npa_context_connect() failed\n"
    assert format_parec_error(1, stderr) == (
        " (código de salida 1; "
        "Connection failure: Connection refused | pa_context_connect() failed)"
    )
    assert format_parec_error(0, "") == ""
    assert format_parec_error(None, "advertencia") == " (advertencia)"


def test_parec_error_suffix_reads_real_stderr():
    class FakeProcess:
        returncode = 2

        @staticmethod
        def poll():
            return 2

    stderr_file = io.BytesIO("Failed to connect to server\n".encode())
    assert StreamController._parec_error_suffix(FakeProcess(), stderr_file) == (
        " (código de salida 2; Failed to connect to server)"
    )


def test_log_repeat_counter_groups_only_consecutive_messages():
    counter = LogRepeatCounter()
    assert counter.register("a") == (False, 1)
    assert counter.register("a") == (True, 2)
    assert counter.register("a") == (True, 3)
    assert counter.register("b") == (False, 1)
    assert counter.register("a") == (False, 1)
    counter.reset()
    assert counter.register("a") == (False, 1)


def _controller(monkeypatch, on_log=None):
    monkeypatch.setattr(
        speakerphone_linux, "detect_pipewire_native_rate", lambda: 48000
    )
    def noop(*_args):
        return None

    return StreamController(noop, noop, on_log or noop, noop)


def test_mute_preserves_gain_level(monkeypatch):
    controller = _controller(monkeypatch)
    controller.set_gain_db(-3.0)
    assert controller.gain_db == -3.0
    assert controller.gain_linear == pytest.approx(10 ** (-3.0 / 20.0))

    controller.set_muted(True)
    assert controller.muted is True
    assert controller.gain_linear == 0.0

    controller.set_gain_db(2.0)
    assert controller.gain_db == 2.0
    assert controller.gain_linear == 0.0

    controller.set_muted(False)
    assert controller.muted is False
    assert controller.gain_linear == pytest.approx(10 ** (2.0 / 20.0))


def test_source_mode_keeps_readable_label(monkeypatch):
    logs = []
    controller = _controller(monkeypatch, on_log=logs.append)
    controller.set_source_mode(
        "__virtual__", "[Virtual] Telefono-USB (usb_phone)"
    )
    assert controller.source_label == "[Virtual] Telefono-USB (usb_phone)"
    assert logs and "Telefono-USB" in logs[-1]
