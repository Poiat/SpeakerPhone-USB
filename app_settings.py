"""Ajustes persistentes de usuario, compartidos por las apps de escritorio."""

import json
import os
import sys
import threading

APP_NAME = "SpeakerPhone-USB"

_lock = threading.Lock()
_cache = None


def settings_dir():
    """Carpeta de configuración según la plataforma."""
    override = os.environ.get("SPEAKERPHONE_CONFIG_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))

    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if not base:
            base = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, APP_NAME)

    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"), APP_NAME
        )

    base = os.environ.get("XDG_CONFIG_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


def settings_path():
    return os.path.join(settings_dir(), "settings.json")


def load_settings():
    """Devuelve una copia de los ajustes guardados (o un dict vacío)."""
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        try:
            with open(settings_path(), encoding="utf-8") as handle:
                data = json.load(handle)
            _cache = data if isinstance(data, dict) else {}
        except Exception:
            _cache = {}
        return dict(_cache)


def save_settings(values):
    """Guarda los ajustes de forma atómica. Devuelve True si tuvo éxito."""
    global _cache
    with _lock:
        _cache = dict(values)
        path = settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temporary = path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(_cache, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
            return True
        except Exception:
            return False


def get_setting(key, default=None):
    return load_settings().get(key, default)


def set_setting(key, value):
    data = load_settings()
    data[key] = value
    return save_settings(data)
