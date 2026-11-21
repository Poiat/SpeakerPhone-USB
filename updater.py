"""Descarga, verificación y aplicación en vivo de actualizaciones."""

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass

from app_version import APP_VERSION
from update_checker import ssl_context

CHUNK_SIZE = 256 * 1024
CREATE_NO_WINDOW = 0x08000000


class UpdateError(Exception):
    pass


class UpdateCancelled(Exception):
    pass


@dataclass
class ApplyResult:
    ok: bool
    message: str
    restart_launched: bool = False
    folder: str = ""


def parse_checksums(text):
    """Convierte SHA256SUMS.txt en {nombre: sha256}."""
    result = {}
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        checksum = parts[0].lower()
        name = parts[-1].lstrip("*")
        if name.startswith("./"):
            name = name[2:]
        if len(checksum) == 64:
            result[name] = checksum
    return result


def _request(url):
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": f"SpeakerPhone-USB/{APP_VERSION}",
            "Accept": "application/octet-stream",
        },
    )


def _fetch_text(url, timeout=20):
    with urllib.request.urlopen(
        _request(url), timeout=timeout, context=ssl_context()
    ) as response:
        return response.read().decode("utf-8", errors="replace")


def download_update(update, progress_cb=None, cancel_event=None, timeout=30):
    """
    Descarga el asset de la actualización a una carpeta temporal, verifica
    tamaño/SHA-256 y devuelve (ruta_descarga, carpeta_temporal).
    """
    url = update.get("asset_url")
    if not url:
        raise UpdateError("La release no incluye un paquete para esta plataforma.")

    asset_name = os.path.basename(update.get("asset_name") or "update.bin")
    workdir = tempfile.mkdtemp(prefix="speakerphone-update-")
    destination = os.path.join(workdir, asset_name)

    try:
        hasher = hashlib.sha256()
        written = 0
        with urllib.request.urlopen(
            _request(url), timeout=timeout, context=ssl_context()
        ) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if total <= 0:
                total = int(update.get("asset_size") or 0)

            with open(destination, "wb") as handle:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise UpdateCancelled()
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    written += len(chunk)
                    if progress_cb is not None:
                        progress_cb(written, total)

        expected_size = int(update.get("asset_size") or 0)
        if expected_size > 0 and written != expected_size:
            raise UpdateError(
                f"Descarga incompleta: {written} de {expected_size} bytes."
            )

        checksums_url = update.get("checksums_url")
        if checksums_url:
            try:
                checksums = parse_checksums(_fetch_text(checksums_url))
            except Exception as exc:
                raise UpdateError(f"No se pudo descargar SHA256SUMS.txt: {exc}") from exc
            expected = checksums.get(asset_name)
            if expected is None:
                raise UpdateError(
                    f"SHA256SUMS.txt no contiene una entrada para {asset_name}."
                )
            if hasher.hexdigest() != expected:
                raise UpdateError("La verificación SHA-256 del paquete falló.")

        return destination, workdir
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def _clean_launch_env():
    env = os.environ.copy()
    original = env.pop("APPIMAGE_ORIG_LD_LIBRARY_PATH", None)
    for key in ("APPIMAGE", "APPDIR", "OWD", "ARGV0", "ARGV1"):
        env.pop(key, None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    elif "APPIMAGE" in os.environ:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _mac_bundle_path():
    executable = os.path.realpath(sys.executable)
    marker = ".app/Contents/MacOS/"
    index = executable.find(marker)
    if index < 0:
        return None
    return executable[: index + len(".app")]


def can_self_update():
    """True si esta ejecución puede reemplazarse y relanzarse sola."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("APPIMAGE"))
    if sys.platform.startswith("win"):
        return bool(getattr(sys, "frozen", False))
    if sys.platform == "darwin":
        return _mac_bundle_path() is not None
    return False


def _apply_linux(download_path, asset_name):
    target = os.environ.get("APPIMAGE")
    if not target:
        return ApplyResult(
            False,
            "La app no se ejecuta desde un AppImage; descarga guardada.",
            folder=os.path.dirname(download_path),
        )

    target = os.path.realpath(target)
    try:
        temporary = target + ".update"
        shutil.copy2(download_path, temporary)
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
        subprocess.Popen(
            [target], env=_clean_launch_env(), start_new_session=True, close_fds=True
        )
        return ApplyResult(
            True,
            f"Actualización aplicada en {target}. Reiniciando…",
            restart_launched=True,
        )
    except Exception as exc:
        return ApplyResult(False, f"No se pudo aplicar: {exc}", folder=os.path.dirname(download_path))


def _apply_windows(download_path, asset_name):
    if not getattr(sys, "frozen", False):
        return ApplyResult(
            False,
            "La app no está empaquetada; descarga guardada.",
            folder=os.path.dirname(download_path),
        )

    target = os.path.realpath(sys.executable)
    new_file = target + ".new"
    try:
        shutil.copy2(download_path, new_file)
    except Exception as exc:
        return ApplyResult(False, f"No se pudo preparar el paquete: {exc}", folder=os.path.dirname(download_path))

    batch_path = os.path.join(tempfile.gettempdir(), "speakerphone-update.cmd")
    batch = f"""@echo off
setlocal
set /a tries=0
:retry
move /y "{new_file}" "{target}" >nul 2>&1
if not errorlevel 1 goto done
set /a tries+=1
if %tries% geq 60 goto failed
timeout /t 1 /nobreak >nul
goto retry
:done
start "" "{target}"
del "%~f0"
exit /b 0
:failed
del "%~f0"
exit /b 1
"""
    try:
        with open(batch_path, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(batch)
        subprocess.Popen(
            ["cmd", "/c", batch_path],
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
            env=os.environ.copy(),
        )
        return ApplyResult(
            True,
            "Actualización preparada. Reiniciando…",
            restart_launched=True,
        )
    except Exception as exc:
        return ApplyResult(False, f"No se pudo preparar el reinicio: {exc}", folder=os.path.dirname(download_path))


def _apply_macos(download_path, asset_name):
    target = _mac_bundle_path()
    if not target:
        return ApplyResult(
            False,
            "La app no se ejecuta desde un bundle .app; descarga guardada.",
            folder=os.path.dirname(download_path),
        )

    workdir = tempfile.mkdtemp(prefix="speakerphone-update-app-")
    try:
        subprocess.run(
            ["ditto", "-x", "-k", download_path, workdir],
            check=True,
            capture_output=True,
        )
        new_bundle = None
        for root, dirs, _files in os.walk(workdir):
            for directory in dirs:
                if directory.endswith(".app"):
                    new_bundle = os.path.join(root, directory)
                    break
            if new_bundle:
                break
        if not new_bundle:
            shutil.rmtree(workdir, ignore_errors=True)
            return ApplyResult(False, "El zip no contiene una app .app.", folder=os.path.dirname(download_path))

        if not os.access(os.path.dirname(target), os.W_OK):
            shutil.rmtree(workdir, ignore_errors=True)
            return ApplyResult(
                False,
                "Sin permisos para reemplazar la app instalada.",
                folder=os.path.dirname(download_path),
            )

        script_path = os.path.join(tempfile.gettempdir(), "speakerphone-update.sh")
        script = f"""#!/bin/sh
sleep 2
rm -rf {shlex.quote(target)}
mv {shlex.quote(new_bundle)} {shlex.quote(target)}
open {shlex.quote(target)}
rm -rf {shlex.quote(workdir)}
rm -f {shlex.quote(script_path)}
"""
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(script_path, 0o755)
        subprocess.Popen(["/bin/sh", script_path], start_new_session=True, close_fds=True)
        return ApplyResult(
            True,
            "Actualización preparada. Reiniciando…",
            restart_launched=True,
        )
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return ApplyResult(False, f"No se pudo aplicar: {exc}", folder=os.path.dirname(download_path))


def apply_update(download_path, asset_name):
    """Aplica el paquete y lanza el reinicio cuando es posible."""
    if sys.platform.startswith("linux"):
        return _apply_linux(download_path, asset_name)
    if sys.platform.startswith("win"):
        return _apply_windows(download_path, asset_name)
    if sys.platform == "darwin":
        return _apply_macos(download_path, asset_name)
    return ApplyResult(False, "Plataforma no soportada.", folder=os.path.dirname(download_path))
