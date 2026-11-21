import json

import app_settings


def reset(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEAKERPHONE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(app_settings, "_cache", None)
    return tmp_path / "settings.json"


def test_roundtrip(monkeypatch, tmp_path):
    path = reset(monkeypatch, tmp_path)
    assert app_settings.load_settings() == {}
    assert app_settings.save_settings({"gain_db": -3.5, "profile": "stable"})
    assert path.exists()
    assert app_settings.load_settings() == {"gain_db": -3.5, "profile": "stable"}
    assert json.loads(path.read_text(encoding="utf-8"))["profile"] == "stable"


def test_setting_helpers(monkeypatch, tmp_path):
    reset(monkeypatch, tmp_path)
    app_settings.set_setting("soft_limiter", False)
    assert app_settings.get_setting("soft_limiter") is False
    assert app_settings.get_setting("missing", "default") == "default"


def test_invalid_file_falls_back(monkeypatch, tmp_path):
    path = reset(monkeypatch, tmp_path)
    path.write_text("no es json", encoding="utf-8")
    assert app_settings.load_settings() == {}
