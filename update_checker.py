#!/usr/bin/env python3
"""
Comprobación de actualizaciones de SpeakerPhone-USB en GitHub Releases.

Solo consulta metadatos (no descarga ni ejecuta binarios). La consulta se
realiza bajo demanda, normalmente desde un hilo en segundo plano para no
bloquear la interfaz.
"""

import json
import re
import ssl
import urllib.request

from app_version import APP_VERSION

try:
    import certifi
except ImportError:  # PyInstaller lo incluye cuando está en requirements.txt
    certifi = None

REPO = "Poiat/SpeakerPhone-USB"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
DEFAULT_TIMEOUT = 6.0


def normalize_version(value):
    """Devuelve 'X.Y.Z' a partir de 'vX.Y.Z', '0.17' o un texto similar."""
    match = re.search(r"(?<!\d)(\d+)(?:\.(\d+))?(?:\.(\d+))?(?!\d)", str(value or ""))
    if not match:
        return ""
    parts = [int(part) if part is not None else 0 for part in match.groups()]
    return ".".join(str(part) for part in parts)


def version_tuple(value):
    """Convierte una versión en una tupla comparable; inválida -> (0, 0, 0)."""
    normalized = normalize_version(value)
    if not normalized:
        return (0, 0, 0)
    return tuple(int(part) for part in normalized.split("."))


def _ssl_context():
    """Contexto TLS con el bundle de certifi si está disponible."""
    if certifi is not None:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
    return ssl.create_default_context()


def check_for_update(current=APP_VERSION, timeout=DEFAULT_TIMEOUT):
    """
    Consulta la última release publicada en GitHub.

    Devuelve un diccionario con:
      status:  'update' | 'current' | 'error'
      current: versión local normalizada
      latest:  versión remota normalizada ('' si no se pudo obtener)
      url:     página de la release para descargar
      error:   detalle del fallo (None si todo fue bien)
    """
    current_norm = normalize_version(current) or str(current)
    result = {
        "status": "error",
        "current": current_norm,
        "latest": "",
        "url": RELEASES_URL,
        "error": None,
    }

    request = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": f"SpeakerPhone-USB/{current_norm}",
            "Accept": "application/vnd.github+json",
        },
    )

    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context()
        ) as response:
            payload = json.load(response)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    latest_norm = normalize_version(payload.get("tag_name", ""))
    if not latest_norm:
        result["error"] = "La respuesta de GitHub no contiene una versión válida."
        return result

    result["latest"] = latest_norm
    result["url"] = payload.get("html_url") or RELEASES_URL
    if version_tuple(latest_norm) > version_tuple(current_norm):
        result["status"] = "update"
    else:
        result["status"] = "current"
    return result
