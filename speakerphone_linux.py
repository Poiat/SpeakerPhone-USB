#!/usr/bin/env python3
"""
USB Audio Stream GUI v4
Transmite el audio del PC (PipeWire/PulseAudio) a Android por cable USB.
Novedades v4:
- Mejoras de audio:
  * Control de Ganancia (-12 dB a +6 dB) y Limitador Suave (Soft Limiter) en tiempo real para evitar distorsión/clipping
  * Selector de frecuencia de muestreo (Nativa de PipeWire, 44100 Hz, 48000 Hz, 96000 Hz)
  * Perfiles de latencia/estabilidad (Baja latencia 8 ms, Equilibrado 15 ms, Alta estabilidad 30 ms) + ajuste manual
- Funcionalidad:
  * Reconexión silenciosa automática al suspender/reanudar (systemd-logind D-Bus / gdbus / dbus-monitor) o desbloquear pantalla
- Interfaz (UI):
  * Layout fluido y adaptable que se ajusta naturalmente al tamaño de la ventana sin escalar tipografías artificialmente
"""
from collections import deque
import json
import math
from string import Template
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from app_logger import (
    get_log_path,
    install_exception_hooks,
    log,
    log_exception,
    log_startup_info,
    open_log_folder,
    setup_logging,
)
from app_icon import get_icon_path
from app_settings import load_settings, save_settings
from app_version import APP_VERSION
from dsp import process_pcm_s16le
from protocol import pack_header
from theme import get_theme, valid_theme
from update_checker import RELEASES_URL, check_for_update
from updater import UpdateCancelled, apply_update, can_self_update, download_update

PKG = "com.usbaudio"
DEFAULT_PORT = 5000
DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_CAPTURE_MS = 15
DEFAULT_CHUNK_MS = 15
MAX_LOG_LINES = 300


def clean_env():
    """Restaura el entorno original del host para evitar conflictos con AppImage/LD_LIBRARY_PATH."""
    env = os.environ.copy()
    if "APPIMAGE_ORIG_LD_LIBRARY_PATH" in env:
        orig = env.pop("APPIMAGE_ORIG_LD_LIBRARY_PATH")
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    elif "APPIMAGE" in env:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def run_cmd(cmd, timeout=None):
    return subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=clean_env(),
    ).stdout.strip()


def check_dependencies():
    return [tool for tool in ("adb", "parec", "pactl") if not shutil.which(tool)]


def qt_binding_version(binding):
    """Devuelve la versión del binding Qt indicado, o 'desconocida'."""
    try:
        module = __import__(binding)
        version = getattr(module, "__version__", None)
        if version:
            return str(version)
        core = __import__(
            f"{binding}.QtCore", fromlist=["PYQT_VERSION_STR", "__version__"]
        )
        for attr in ("PYQT_VERSION_STR", "__version__"):
            value = getattr(core, attr, None)
            if value:
                return str(value)
    except Exception:
        pass
    return "desconocida"


def check_adb_health():
    """
    Comprueba si ADB está instalado y realmente funciona (binario ejecutable y
    servidor capaz de arrancar/responde). Devuelve (estado, detalle), donde
    estado es 'ok', 'missing' o 'error'.
    """
    path = shutil.which("adb")
    if not path:
        return "missing", (
            "ADB no está instalado. Instálalo con: "
            "sudo pacman -S --needed android-tools"
        )
    try:
        version_out = run_cmd(["adb", "version"], timeout=5)
        version = ""
        m = re.search(r"^Version\s+(\S+)", version_out, re.MULTILINE)
        if m:
            version = f" v{m.group(1)}"
        run_cmd(["adb", "start-server"], timeout=10)
        return "ok", f"ADB{version} instalado y funcionando ({path})."
    except subprocess.TimeoutExpired:
        return "error", "ADB está instalado pero no responde (tiempo de espera agotado)."
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        detail = err.splitlines()[-1] if err else f"código de salida {e.returncode}"
        return "error", f"ADB está instalado pero no funciona: {detail}"
    except FileNotFoundError:
        return "missing", "ADB no está instalado o el ejecutable no es accesible."
    except Exception as e:
        return "error", f"ADB está instalado pero no funciona: {e}"


def detect_pipewire_native_rate():
    """Detecta la frecuencia nativa actual del servidor PipeWire/PulseAudio."""
    try:
        if shutil.which("pw-metadata"):
            out = run_cmd(["pw-metadata", "-n", "settings", "0", "clock.rate"], timeout=2)
            m = re.search(r"value:'(\d+)'", out)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    try:
        out = run_cmd(["pactl", "info"], timeout=2)
        m = re.search(r"Default Sample Specification:.*?(\d+)Hz", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return DEFAULT_RATE


def list_audio_sources():
    """Devuelve lista de tuplas (id_fuente, etiqueta_legible) disponibles en PipeWire/PulseAudio."""
    options = [
        ("__default__", "Salida por defecto del sistema (monitor automático)"),
        ("__virtual__", "Sink virtual 'Telefono-USB' (silenciar PC)"),
    ]
    try:
        out = run_cmd(["pactl", "list", "short", "sources"], timeout=3)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[1].strip()
                if name == "usb_phone.monitor":
                    continue
                tag = "Monitor" if name.endswith(".monitor") else "Entrada"
                options.append((name, f"[{tag}] {name}"))
    except Exception:
        pass
    return options


def query_adb_device():
    """Consulta `adb devices -l` y devuelve (estado, descripción) del mejor dispositivo."""
    try:
        out = run_cmd(["adb", "devices", "-l"], timeout=3)
    except Exception as e:
        return "error", f"Error ejecutando adb: {e}"

    entries = []
    for raw_line in out.splitlines()[1:]:
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"

        model_match = re.search(r"model:([^\s]+)", line)
        device_match = re.search(r"device:([^\s]+)", line)
        label = None
        if model_match:
            label = model_match.group(1).replace("_", " ")
        elif device_match:
            label = device_match.group(1)

        display = f"{label} ({serial})" if label else serial
        entries.append((state, display))

    if not entries:
        return "none", "Ningún dispositivo conectado"

    # Preferir un dispositivo listo; si no, uno pendiente de autorización.
    chosen = next((entry for entry in entries if entry[0] == "device"), None)
    if chosen is None:
        chosen = next((entry for entry in entries if entry[0] == "unauthorized"), None)
    if chosen is None:
        chosen = entries[0]

    state, display = chosen
    if state == "device":
        return "ready", display
    if state == "unauthorized":
        return "unauthorized", f"{display} — Pendiente de autorización USB"
    if state == "offline":
        return "offline", f"{display} — Fuera de línea (offline)"
    return state, f"{display} ({state})"


class SleepLockWatcher:
    """
    Observa señales de suspensión/reanudación (org.freedesktop.login1.Manager PrepareForSleep)
    y bloqueo/desbloqueo de pantalla (org.freedesktop.ScreenSaver ActiveChanged)
    para ejecutar una reconexión silenciosa.
    """

    def __init__(self, on_sleep_change, on_wake_or_unlock, on_log):
        self.on_sleep_change = on_sleep_change
        self.on_wake_or_unlock = on_wake_or_unlock
        self.on_log = on_log
        self._stop = threading.Event()
        self._procs = []
        self._threads = []

    def start(self):
        if not shutil.which("gdbus"):
            return
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        t1 = threading.Thread(target=self._watch_system_sleep, daemon=True)
        t2 = threading.Thread(target=self._watch_session_lock, daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()

    def stop(self):
        self._stop.set()
        for p in list(self._procs):
            try:
                p.terminate()
            except Exception:
                pass
        self._procs.clear()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []

    def _watch_system_sleep(self):
        cmd = [
            "gdbus", "monitor", "--system",
            "--dest", "org.freedesktop.login1",
            "--object-path", "/org/freedesktop/login1",
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, env=clean_env()
            )
            self._procs.append(proc)
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                if "PrepareForSleep" in line:
                    if "(true,)" in line:
                        self.on_log("Sistema entrando en suspensión: pausando audio...")
                        self.on_sleep_change(True)
                    elif "(false,)" in line:
                        self.on_log("Sistema reanudado: reconectando silenciosamente...")
                        self.on_sleep_change(False)
                        self.on_wake_or_unlock()
        except Exception:
            pass

    def _watch_session_lock(self):
        cmd = [
            "gdbus", "monitor", "--session",
            "--dest", "org.freedesktop.ScreenSaver",
            "--object-path", "/ScreenSaver",
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, env=clean_env()
            )
            self._procs.append(proc)
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                if "ActiveChanged" in line and "(false,)" in line:
                    self.on_log("Sesión desbloqueada: verificando túnel USB...")
                    self.on_wake_or_unlock()
        except Exception:
            pass


class StreamController:
    """Controlador en segundo plano que gestiona ADB, sink virtual, DSP PCM y streaming TCP."""

    def __init__(self, on_status, on_device, on_log, on_level):
        self.on_status = on_status
        self.on_device = on_device
        self.on_log = on_log
        self.on_level = on_level

        self.port = DEFAULT_PORT
        self.rate_mode = "native"  # "native" | "44100" | "48000" | "96000"
        self.rate = detect_pipewire_native_rate()
        self.channels = DEFAULT_CHANNELS
        self.capture_ms = DEFAULT_CAPTURE_MS
        self.chunk_ms = DEFAULT_CHUNK_MS
        self.source_mode = "__default__"

        self.gain_db = 0.0
        self.gain_linear = 1.0
        self.soft_limiter = True

        self._running = False
        self._suspended = False
        self._retry_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread = None
        self._monitor_thread = None

        self._active_sock = None
        self._active_proc = None
        self._prev_sink = None
        self._null_module = None
        self._lock = threading.Lock()

        self.sleep_watcher = SleepLockWatcher(
            on_sleep_change=self._handle_sleep_state,
            on_wake_or_unlock=self.silent_reconnect,
            on_log=self.on_log,
        )

    def _cleanup_orphan_virtual_sink(self):
        """Libera sinks virtuales 'usb_phone' que quedaron tras un cierre brusco."""
        try:
            procs = subprocess.run(
                ["pgrep", "-af", "parec"],
                capture_output=True,
                text=True,
                timeout=2,
                env=clean_env(),
            ).stdout
            if "usb_phone" in procs:
                return  # Otra instancia está transmitiendo: no tocar su sink.
        except Exception:
            pass

        try:
            out = run_cmd(["pactl", "list", "short", "modules"], timeout=3)
        except Exception:
            return

        released = False
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            module_id = parts[0].strip()
            if "module-null-sink" in parts[1] and "usb_phone" in line:
                try:
                    subprocess.run(
                        ["pactl", "unload-module", module_id],
                        capture_output=True,
                        timeout=2,
                        env=clean_env(),
                    )
                    released = True
                except Exception:
                    pass
        if released:
            self.on_log("Sink virtual huérfano 'usb_phone' liberado.")

        try:
            if run_cmd(["pactl", "get-default-sink"], timeout=2) == "usb_phone":
                sinks = run_cmd(["pactl", "list", "short", "sinks"], timeout=2)
                for line in sinks.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2 and parts[1].strip() != "usb_phone":
                        run_cmd(["pactl", "set-default-sink", parts[1].strip()], timeout=2)
                        self.on_log(f"Sink por defecto restaurado a '{parts[1].strip()}'.")
                        break
        except Exception:
            pass

    def start(self):
        if self._running:
            return
        self._running = True
        self._suspended = False
        self._stop_event.clear()
        self._retry_event.clear()

        self._cleanup_orphan_virtual_sink()
        self.sleep_watcher.start()

        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_thread = threading.Thread(target=self._device_poll_loop, daemon=True)
            self._monitor_thread.start()

    def stop(self):
        self._running = False
        self._stop_event.set()
        self._retry_event.set()
        self.sleep_watcher.stop()
        self._abort_active_io()
        self._cleanup_virtual_sink()
        self._remove_adb_forward()
        self.on_level(0.0, 0)
        self.on_status("stopped", "Desconectado (transmisión detenida)")
        self.on_log("Transmisión detenida.")

    def _handle_sleep_state(self, is_sleeping):
        self._suspended = is_sleeping
        if is_sleeping:
            self._abort_active_io()
            self._cleanup_virtual_sink()
            self._remove_adb_forward()
            self.on_level(0.0, 0)
            self.on_status("stopped", "En pausa por suspensión del sistema")

    def silent_reconnect(self):
        """Reconexión silenciosa tras despertar o desbloquear sin molestar al usuario."""
        if not self._running or self._suspended:
            return
        self._abort_active_io()
        self._remove_adb_forward()
        self._retry_event.set()

    def retry_now(self):
        self.on_log("Reiniciando conexión...")
        self._suspended = False
        self._abort_active_io()
        self._remove_adb_forward()
        self.on_level(0.0, 0)
        if not self._running:
            self.start()
        else:
            self._retry_event.set()

    def set_gain_db(self, db):
        self.gain_db = max(-12.0, min(6.0, float(db)))
        self.gain_linear = math.pow(10.0, self.gain_db / 20.0)

    def set_soft_limiter(self, enabled):
        self.soft_limiter = bool(enabled)
        self.on_log(f"Limitador suave {'activado' if enabled else 'desactivado'}.")

    def set_rate_mode(self, mode):
        if self.rate_mode == mode:
            return
        self.rate_mode = mode
        if mode == "native":
            self.rate = detect_pipewire_native_rate()
        else:
            self.rate = int(mode)
        self.on_log(f"Frecuencia de muestreo: {self.rate} Hz ({'nativa' if mode == 'native' else 'manual'})")
        if self._running:
            self.retry_now()

    def set_source_mode(self, mode):
        if self.source_mode == mode:
            return
        self.source_mode = mode
        self.on_log(f"Fuente seleccionada: {mode}")
        if self._running:
            self.retry_now()

    def set_latency(self, capture_ms, chunk_ms):
        capture_ms = max(2, min(100, int(capture_ms)))
        chunk_ms = max(2, min(100, int(chunk_ms)))
        if self.capture_ms == capture_ms and self.chunk_ms == chunk_ms:
            return
        self.capture_ms = capture_ms
        self.chunk_ms = chunk_ms
        self.on_log(f"Latencia ajustada: captura={capture_ms} ms, paquete={chunk_ms} ms")
        if self._running:
            self.retry_now()

    def _abort_active_io(self):
        with self._lock:
            if self._active_proc is not None:
                try:
                    self._active_proc.terminate()
                except Exception:
                    pass
                self._active_proc = None
            if self._active_sock is not None:
                try:
                    self._active_sock.close()
                except Exception:
                    pass
                self._active_sock = None

    def _remove_adb_forward(self):
        try:
            subprocess.run(
                ["adb", "forward", "--remove", f"tcp:{self.port}"],
                capture_output=True,
                timeout=2,
                env=clean_env(),
            )
        except Exception:
            pass

    def _setup_virtual_sink(self):
        self._cleanup_virtual_sink()
        prev = run_cmd(["pactl", "get-default-sink"])
        mod = run_cmd([
            "pactl", "load-module", "module-null-sink",
            "sink_name=usb_phone",
            "sink_properties=device.description=Telefono-USB",
            f"rate={self.rate}",
            f"channels={self.channels}",
        ])
        run_cmd(["pactl", "set-default-sink", "usb_phone"])
        self._prev_sink = prev
        self._null_module = mod
        self.on_log(f"Sink virtual 'Telefono-USB' creado (anterior: {prev}).")
        return "usb_phone.monitor"

    def _cleanup_virtual_sink(self):
        if self._prev_sink:
            try:
                subprocess.run(
                    ["pactl", "set-default-sink", self._prev_sink],
                    capture_output=True,
                    timeout=2,
                    env=clean_env(),
                )
            except Exception:
                pass
            self._prev_sink = None
        if self._null_module:
            try:
                subprocess.run(
                    ["pactl", "unload-module", self._null_module],
                    capture_output=True,
                    timeout=2,
                    env=clean_env(),
                )
            except Exception:
                pass
            self._null_module = None

    def _resolve_source(self):
        if self.source_mode == "__virtual__":
            return self._setup_virtual_sink()
        if self.source_mode == "__default__":
            return run_cmd(["pactl", "get-default-sink"]) + ".monitor"
        return self.source_mode

    def _device_poll_loop(self):
        last_disp = None
        while not self._stop_event.is_set():
            if not self._suspended:
                state, disp = query_adb_device()
                if disp != last_disp:
                    last_disp = disp
                    self.on_device(state, disp)
            time.sleep(2.0 if self._running else 3.0)

    def _worker_loop(self):
        attempt = 0
        while self._running and not self._stop_event.is_set():
            self._retry_event.clear()
            if self._suspended:
                self._wait_or_retry(1.0)
                continue

            try:
                if self.rate_mode == "native":
                    self.rate = detect_pipewire_native_rate()

                state, disp = query_adb_device()
                self.on_device(state, disp)

                if state == "none":
                    self.on_status("waiting_usb", "Esperando dispositivo USB...")
                    if attempt == 0:
                        self.on_log("Conecta el móvil Android por cable USB con Depuración USB activa.")
                    self._wait_or_retry(1.5)
                    attempt += 1
                    continue

                if state == "unauthorized":
                    self.on_status("unauthorized", "Acepta el aviso de depuración USB en el móvil")
                    self.on_log("Dispositivo no autorizado: revisa la pantalla del móvil.")
                    self._wait_or_retry(2.0)
                    attempt += 1
                    continue

                if state != "ready":
                    self.on_status("waiting_usb", f"Esperando dispositivo listo ({state})...")
                    self._wait_or_retry(1.5)
                    attempt += 1
                    continue

                self.on_status("connecting", "Configurando túnel USB y conectando con la app...")
                run_cmd(["adb", "forward", f"tcp:{self.port}", f"tcp:{self.port}"], timeout=4)

                if attempt % 5 == 0:
                    try:
                        run_cmd([
                            "adb", "shell", "am", "start",
                            "-n", f"{PKG}/.MainActivity",
                            "--ez", "autostart", "true",
                        ], timeout=4)
                        self.on_log("Lanzando app com.usbaudio en el teléfono...")
                    except Exception:
                        pass

                source = self._resolve_source()
                self._stream_audio(source)
                attempt = 0

            except Exception as e:
                self._abort_active_io()
                self._cleanup_virtual_sink()
                self.on_level(0.0, 0)
                if not self._running or self._stop_event.is_set():
                    break
                if self._suspended:
                    continue
                msg = str(e).strip() or e.__class__.__name__
                self.on_status("retrying", f"Reintentando conexión ({msg})")
                self.on_log(f"[reintento] {msg}")
                attempt += 1
                self._wait_or_retry(1.2)

    def _wait_or_retry(self, seconds):
        self._retry_event.wait(timeout=seconds)

    def _stream_audio(self, source):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
        with self._lock:
            self._active_sock = sock

        try:
            ack = sock.recv(1)
            if ack != b"R":
                raise ConnectionError("La app Android no respondió 'R'")
        except socket.timeout:
            raise ConnectionError("La app Android no está escuchando (¿está abierta en el móvil?)")

        sock.settimeout(None)
        sock.sendall(pack_header(self.rate, self.channels, self.chunk_ms))

        chunk = self.channels * 2 * self.rate * self.chunk_ms // 1000
        cap = subprocess.Popen(
            [
                "parec",
                "--device", source,
                "--format=s16le",
                f"--rate={self.rate}",
                f"--channels={self.channels}",
                f"--latency-msec={self.capture_ms}",
                "--client-name=usb-audio-stream",
                "--stream-name=usb-audio",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=clean_env(),
        )
        with self._lock:
            self._active_proc = cap

        self.on_status(
            "streaming",
            f"Transmitiendo ({self.rate} Hz • {self.capture_ms}/{self.chunk_ms} ms)"
        )
        self.on_log(f"Transmitiendo '{source}' a {self.rate} Hz -> tcp:{self.port}")

        last_ui_push = 0.0
        clip_hold_until = 0.0
        read_fn = os.read
        sendall_fn = sock.sendall
        mono_fn = time.monotonic

        try:
            fd = cap.stdout.fileno()
            while self._running and not self._stop_event.is_set() and not self._suspended:
                data = read_fn(fd, chunk)
                if not data:
                    raise ConnectionError("parec finalizó la captura")

                now = mono_fn()
                need_meter = (now - last_ui_push) >= 0.06
                out_data, lvl, clip = process_pcm_s16le(
                    data, self.gain_linear, self.soft_limiter, need_meter
                )
                sendall_fn(out_data)

                if need_meter:
                    if clip:
                        clip_hold_until = now + 0.65
                    self.on_level(float(lvl), 1 if (now < clip_hold_until) else 0)
                    last_ui_push = now
        finally:
            self._abort_active_io()
            self._cleanup_virtual_sink()
            self.on_level(0.0, 0)


def run_qt_app(qt_binding):
    if qt_binding == "PySide6":
        from PySide6.QtCore import Qt, Signal, QObject, QUrl, QTimer, QEvent
        from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QDesktopServices
        from PySide6.QtWidgets import (
            QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
            QSizePolicy, QLabel, QPushButton, QComboBox, QSpinBox, QSlider,
            QCheckBox, QFrame, QPlainTextEdit, QProgressBar, QSystemTrayIcon,
            QMenu, QScrollArea, QDialog
        )
    else:
        from PyQt6.QtCore import Qt, pyqtSignal as Signal, QObject, QUrl, QTimer, QEvent
        from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QDesktopServices
        from PyQt6.QtWidgets import (
            QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
            QSizePolicy, QLabel, QPushButton, QComboBox, QSpinBox, QSlider,
            QCheckBox, QFrame, QPlainTextEdit, QProgressBar, QSystemTrayIcon,
            QMenu, QScrollArea, QDialog
        )

    _icon_cache = {}
    _base_icon_loaded = False
    _base_icon = None

    def get_base_icon():
        nonlocal _base_icon_loaded, _base_icon
        if not _base_icon_loaded:
            _base_icon_loaded = True
            path = get_icon_path()
            if path:
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    _base_icon = pixmap.scaled(
                        64,
                        64,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
        return _base_icon

    def make_tray_icon(dot_color_hex):
        if dot_color_hex in _icon_cache:
            return _icon_cache[dot_color_hex]
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        base = get_base_icon()
        if base is not None:
            p.drawPixmap(0, 0, base)
        else:
            p.setBrush(QColor("#1e293b"))
            p.setPen(QColor("#3b82f6"))
            p.drawRoundedRect(4, 4, 56, 56, 14, 14)

            pen = p.pen()
            pen.setColor(QColor("#ffffff"))
            pen.setWidth(5)
            p.setPen(pen)
            p.drawArc(18, 16, 28, 28, 0, 180 * 16)
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(14, 30, 7, 14, 3, 3)
            p.drawRoundedRect(43, 30, 7, 14, 3, 3)

        p.setPen(QColor("#0f172a"))
        p.setBrush(QColor(dot_color_hex))
        p.drawEllipse(42, 42, 18, 18)
        p.end()
        ico = QIcon(pm)
        _icon_cache[dot_color_hex] = ico
        return ico

    class Bridge(QObject):
        # Estas señales se emiten desde hilos Python hacia el hilo de la GUI.
        # PySide6 6.12.0 tiene una regresión de refcount en Signal.emit() que
        # provoca "Fatal Python error: bool_dealloc" con Python < 3.12; por eso
        # requirements.txt fija PySide6==6.11.2.
        status_sig = Signal(str, str)
        device_sig = Signal(str, str)
        log_sig = Signal(str)
        level_sig = Signal(float, int)
        tools_sig = Signal(str, str)
        update_sig = Signal(str)

    class MainWindow(QWidget):
        def __init__(self, app):
            super().__init__()
            self.app = app
            self._force_quit = False
            self._status_color = "#eab308"
            self._is_clipping = False
            self._badge_state = None
            self._pending_level = 0.0
            self._update_url = RELEASES_URL
            self._settings_ready = False
            self._theme = valid_theme(load_settings().get("theme", "dark"))
            self._latest_update = None
            self._update_window = None
            self._auto_prompted_version = ""

            self.setWindowTitle(f"USB Audio Stream v{APP_VERSION}")
            self.setWindowIcon(make_tray_icon("#3b82f6"))
            self.setMinimumSize(420, 440)
            self.resize(540, 620)

            self.bridge = Bridge()
            self.bridge.status_sig.connect(self._update_status)
            self.bridge.device_sig.connect(self._update_device)
            self.bridge.log_sig.connect(self._append_log)
            self.bridge.level_sig.connect(self._update_level, Qt.ConnectionType.QueuedConnection)
            self.bridge.tools_sig.connect(self._update_adb_status)
            self.bridge.update_sig.connect(self._update_check_result)

            self.controller = StreamController(
                on_status=self.bridge.status_sig.emit,
                on_device=self.bridge.device_sig.emit,
                on_log=self.bridge.log_sig.emit,
                on_level=self.bridge.level_sig.emit,
            )

            self._build_ui()
            self._apply_accessibility()
            self._setup_tray()
            self._apply_style()
            self._refresh_sources()
            self._apply_saved_settings()
            self._settings_ready = True

            missing = check_dependencies()
            if missing:
                self._update_status("error", f"Faltan herramientas: {', '.join(missing)}")
                self._append_log(
                    "Instala las dependencias en CachyOS/Arch:\n"
                    "sudo pacman -S --needed android-tools libpulse pipewire-pulse"
                )
            else:
                self.controller.start()

            self._refresh_adb_status()
            self._check_updates()

            self.update_timer = QTimer(self)
            self.update_timer.setInterval(6 * 60 * 60 * 1000)
            self.update_timer.timeout.connect(self._check_updates)
            self.update_timer.start()

        def _build_ui(self):
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            outer.addWidget(scroll)

            container = QWidget()
            scroll.setWidget(container)

            root = QVBoxLayout(container)
            root.setContentsMargins(18, 16, 18, 16)
            root.setSpacing(12)

            # Cabecera
            title = QLabel("USB Audio Stream")
            title.setObjectName("titleLabel")
            version_label = QLabel(f"v{APP_VERSION}")
            version_label.setObjectName("versionLabel")
            version_label.setAccessibleName(f"Versión {APP_VERSION}")
            self.theme_btn = QPushButton("☀ Claro")
            self.theme_btn.setObjectName("miniBtn")
            self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.theme_btn.setToolTip("Cambiar entre tema oscuro y claro")
            self.theme_btn.setAccessibleName("Cambiar tema")
            self.theme_btn.clicked.connect(self._toggle_theme)
            title_row = QHBoxLayout()
            title_row.addWidget(title)
            title_row.addStretch(1)
            title_row.addWidget(self.theme_btn)
            title_row.addWidget(version_label, 0, Qt.AlignmentFlag.AlignVCenter)

            subtitle = QLabel("Audio de PipeWire hacia Android por cable USB (ADB) • Reconexión automática")
            subtitle.setObjectName("subtitle")
            subtitle.setWordWrap(True)

            update_row = QHBoxLayout()
            update_row.setSpacing(8)
            self.update_label = QLabel("Buscando actualizaciones...")
            self.update_label.setObjectName("hintLabel")
            self.update_label.setWordWrap(True)
            self.update_btn = QPushButton("Descargar")
            self.update_btn.setObjectName("miniBtn")
            self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.update_btn.setVisible(False)
            self.update_btn.clicked.connect(self._open_update_window)
            self.update_check_btn = QPushButton("Buscar actualizaciones")
            self.update_check_btn.setObjectName("miniBtn")
            self.update_check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.update_check_btn.setToolTip("Volver a comprobar si hay una versión nueva")
            self.update_check_btn.clicked.connect(self._check_updates)
            update_row.addWidget(self.update_label, 1)
            update_row.addWidget(self.update_check_btn)
            update_row.addWidget(self.update_btn)

            root.addLayout(title_row)
            root.addWidget(subtitle)
            root.addLayout(update_row)

            # Tarjeta 1: Estado y Nivel
            main_card = QFrame()
            main_card.setObjectName("card")
            main_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            mc = QVBoxLayout(main_card)
            mc.setContentsMargins(14, 12, 14, 12)
            mc.setSpacing(8)

            dev_title = QLabel("DISPOSITIVO CONECTADO")
            dev_title.setObjectName("sectionLabel")
            self.device_label = QLabel("Buscando dispositivo...")
            self.device_label.setObjectName("valueLabel")
            self.device_label.setWordWrap(True)
            mc.addWidget(dev_title)
            mc.addWidget(self.device_label)

            sep1 = QFrame()
            sep1.setFrameShape(QFrame.Shape.HLine)
            sep1.setObjectName("sep")
            mc.addWidget(sep1)

            st_title = QLabel("ESTADO DE CONEXIÓN")
            st_title.setObjectName("sectionLabel")
            st_row = QHBoxLayout()
            st_row.setSpacing(8)
            self.dot = QLabel("●")
            self.dot.setFixedWidth(16)
            self.status_label = QLabel("Iniciando...")
            self.status_label.setObjectName("valueLabel")
            self.status_label.setWordWrap(True)
            st_row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
            st_row.addWidget(self.status_label, 1)
            mc.addWidget(st_title)
            mc.addLayout(st_row)

            sep2 = QFrame()
            sep2.setFrameShape(QFrame.Shape.HLine)
            sep2.setObjectName("sep")
            mc.addWidget(sep2)

            adb_hdr = QHBoxLayout()
            adb_hdr.setSpacing(8)
            adb_title = QLabel("ADB (DEPURACIÓN USB)")
            adb_title.setObjectName("sectionLabel")
            self.adb_badge = QLabel("ADB: comprobando...")
            self.adb_badge.setObjectName("adbNeutral")
            self.adb_recheck_btn = QPushButton("↻")
            self.adb_recheck_btn.setObjectName("miniBtn")
            self.adb_recheck_btn.setFixedWidth(30)
            self.adb_recheck_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.adb_recheck_btn.setToolTip("Volver a comprobar si adb está instalado y funciona")
            self.adb_recheck_btn.setAccessibleName("Volver a comprobar ADB")
            self.adb_recheck_btn.clicked.connect(self._refresh_adb_status)
            adb_hdr.addWidget(adb_title)
            adb_hdr.addStretch(1)
            adb_hdr.addWidget(self.adb_badge)
            adb_hdr.addWidget(self.adb_recheck_btn)
            mc.addLayout(adb_hdr)

            self.adb_label = QLabel("Verificando si adb está instalado y responde...")
            self.adb_label.setObjectName("hintLabel")
            self.adb_label.setWordWrap(True)
            mc.addWidget(self.adb_label)

            lvl_header = QHBoxLayout()
            lvl_title = QLabel("NIVEL DE SEÑAL Y SATURACIÓN")
            lvl_title.setObjectName("sectionLabel")
            self.clip_badge = QLabel("SEÑAL OK")
            self.clip_badge.setObjectName("clipOk")
            lvl_header.addWidget(lvl_title)
            lvl_header.addStretch(1)
            lvl_header.addWidget(self.clip_badge)
            mc.addLayout(lvl_header)

            self.level_bar = QProgressBar()
            self.level_bar.setRange(0, 100)
            self.level_bar.setValue(0)
            self.level_bar.setTextVisible(False)
            self.level_bar.setFixedHeight(10)
            self.level_bar.setObjectName("vuNormal")
            mc.addWidget(self.level_bar)

            root.addWidget(main_card)

            # Tarjeta 2: Audio (Fuente, Frecuencia, Ganancia, Limitador y Latencia)
            cfg_card = QFrame()
            cfg_card.setObjectName("card")
            cfg_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            cc = QVBoxLayout(cfg_card)
            cc.setContentsMargins(14, 12, 14, 12)
            cc.setSpacing(10)

            src_title = QLabel("FUENTE DE AUDIO Y FRECUENCIA")
            src_title.setObjectName("sectionLabel")
            cc.addWidget(src_title)

            src_row = QHBoxLayout()
            src_row.setSpacing(8)
            self.source_combo = QComboBox()
            self.source_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.source_combo.setMinimumWidth(140)
            self.source_combo.currentIndexChanged.connect(self._on_source_changed)

            self.refresh_src_btn = QPushButton("↻")
            self.refresh_src_btn.setFixedWidth(36)
            self.refresh_src_btn.setToolTip("Actualizar fuentes de audio")
            self.refresh_src_btn.clicked.connect(self._refresh_sources)

            self.rate_combo = QComboBox()
            native_hz = detect_pipewire_native_rate()
            self.rate_combo.addItem(f"Nativa ({native_hz} Hz)", "native")
            self.rate_combo.addItem("44.1 kHz", "44100")
            self.rate_combo.addItem("48 kHz", "48000")
            self.rate_combo.addItem("96 kHz", "96000")
            self.rate_combo.currentIndexChanged.connect(self._on_rate_changed)

            src_row.addWidget(self.source_combo, 1)
            src_row.addWidget(self.refresh_src_btn)
            src_row.addWidget(self.rate_combo)
            cc.addLayout(src_row)

            # Ganancia + Limitador suave
            gain_row = QHBoxLayout()
            gain_row.setSpacing(8)
            gain_lbl = QLabel("Ganancia:")
            self.gain_slider = QSlider(Qt.Orientation.Horizontal)
            self.gain_slider.setRange(-120, 60)  # -12.0 dB a +6.0 dB
            self.gain_slider.setValue(0)
            self.gain_slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.gain_slider.valueChanged.connect(self._on_gain_changed)

            self.gain_val_lbl = QLabel("0.0 dB")
            self.gain_val_lbl.setMinimumWidth(52)
            self.gain_val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            self.limiter_cb = QCheckBox("Limitador suave")
            self.limiter_cb.setChecked(True)
            self.limiter_cb.setToolTip("Suaviza picos altos antes de enviarlos para evitar distorsión")
            self.limiter_cb.toggled.connect(self._on_limiter_toggled)

            gain_row.addWidget(gain_lbl)
            gain_row.addWidget(self.gain_slider, 1)
            gain_row.addWidget(self.gain_val_lbl)
            gain_row.addWidget(self.limiter_cb)
            cc.addLayout(gain_row)

            # Perfiles rápidos de latencia + ajuste fino adaptable
            lat_grid = QGridLayout()
            lat_grid.setHorizontalSpacing(8)
            lat_grid.setVerticalSpacing(6)

            prof_lbl = QLabel("Perfil:")
            self.profile_combo = QComboBox()
            self.profile_combo.addItem("Baja latencia (8 ms)", ("low", 8, 8))
            self.profile_combo.addItem("Equilibrado (15 ms)", ("balanced", 15, 15))
            self.profile_combo.addItem("Alta estabilidad (30 ms)", ("stable", 30, 30))
            self.profile_combo.setItemData(0, "Mínima latencia (juegos)", Qt.ItemDataRole.ToolTipRole)
            self.profile_combo.setItemData(1, "Balance latencia/estabilidad", Qt.ItemDataRole.ToolTipRole)
            self.profile_combo.setItemData(2, "Mayor margen contra cortes USB", Qt.ItemDataRole.ToolTipRole)
            self.profile_combo.setCurrentIndex(1)
            self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)

            cap_lbl = QLabel("Captura:")
            self.cap_spin = QSpinBox()
            self.cap_spin.setRange(2, 100)
            self.cap_spin.setValue(DEFAULT_CAPTURE_MS)
            self.cap_spin.setSuffix(" ms")

            chk_lbl = QLabel("Paquete:")
            self.chk_spin = QSpinBox()
            self.chk_spin.setRange(2, 100)
            self.chk_spin.setValue(DEFAULT_CHUNK_MS)
            self.chk_spin.setSuffix(" ms")

            self.apply_lat_btn = QPushButton("Aplicar")
            self.apply_lat_btn.clicked.connect(self._on_apply_latency)

            lat_grid.addWidget(prof_lbl, 0, 0)
            lat_grid.addWidget(self.profile_combo, 0, 1, 1, 3)
            lat_grid.addWidget(self.apply_lat_btn, 0, 4)

            lat_grid.addWidget(cap_lbl, 1, 0)
            lat_grid.addWidget(self.cap_spin, 1, 1)
            lat_grid.addWidget(chk_lbl, 1, 2)
            lat_grid.addWidget(self.chk_spin, 1, 3, 1, 2)
            lat_grid.setColumnStretch(1, 1)
            lat_grid.setColumnStretch(3, 1)
            cc.addLayout(lat_grid)

            root.addWidget(cfg_card)

            # Botones principales
            btn_row = QHBoxLayout()
            btn_row.setSpacing(10)

            self.retry_btn = QPushButton("Reintentar conexión")
            self.retry_btn.setObjectName("primaryBtn")
            self.retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.retry_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.retry_btn.clicked.connect(self._on_retry)

            self.toggle_btn = QPushButton("Detener")
            self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.toggle_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            self.toggle_btn.clicked.connect(self._on_toggle)

            btn_row.addWidget(self.retry_btn, 2)
            btn_row.addWidget(self.toggle_btn, 1)
            root.addLayout(btn_row)

            # Log en vivo (absorbe el espacio vertical adicional al agrandar la ventana)
            log_hdr = QHBoxLayout()
            log_lbl = QLabel("LOG EN VIVO")
            log_lbl.setObjectName("sectionLabel")
            open_logs_btn = QPushButton("Abrir carpeta")
            open_logs_btn.setObjectName("miniBtn")
            open_logs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_logs_btn.setToolTip(get_log_path() or "Carpeta de logs no disponible")
            open_logs_btn.setAccessibleName("Abrir carpeta de logs")
            open_logs_btn.clicked.connect(self._open_logs)
            clear_log_btn = QPushButton("Limpiar")
            clear_log_btn.setObjectName("miniBtn")
            clear_log_btn.setAccessibleName("Limpiar registro")
            clear_log_btn.clicked.connect(lambda: self.log_box.clear())
            log_hdr.addWidget(log_lbl)
            log_hdr.addStretch(1)
            log_hdr.addWidget(open_logs_btn)
            log_hdr.addWidget(clear_log_btn)
            root.addLayout(log_hdr)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setMaximumBlockCount(MAX_LOG_LINES)
            self.log_box.setMinimumHeight(80)
            self.log_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.log_box.setPlaceholderText("Eventos de conexión y audio en tiempo real...")
            root.addWidget(self.log_box, 1)

        def _apply_accessibility(self):
            self.setAccessibleName("USB Audio Stream")
            self.device_label.setAccessibleName("Dispositivo conectado")
            self.status_label.setAccessibleName("Estado de conexión")
            self.level_bar.setAccessibleName("Nivel de señal")
            self.clip_badge.setAccessibleName("Estado de saturación")
            self.adb_badge.setAccessibleName("Estado de ADB")
            self.adb_label.setAccessibleName("Detalle del estado de ADB")
            self.update_label.setAccessibleName("Estado de actualizaciones")
            self.update_btn.setAccessibleName("Actualizar aplicación")
            self.update_check_btn.setAccessibleName("Buscar actualizaciones")
            self.source_combo.setAccessibleName("Fuente de audio")
            self.rate_combo.setAccessibleName("Frecuencia de muestreo")
            self.refresh_src_btn.setAccessibleName("Actualizar fuentes de audio")
            self.profile_combo.setAccessibleName("Perfil de búfer")
            self.gain_slider.setAccessibleName("Ganancia")
            self.gain_val_lbl.setAccessibleName("Valor de ganancia")
            self.limiter_cb.setAccessibleName("Limitador suave")
            self.cap_spin.setAccessibleName("Captura en milisegundos")
            self.chk_spin.setAccessibleName("Paquete en milisegundos")
            self.apply_lat_btn.setAccessibleName("Aplicar latencia")
            self.retry_btn.setAccessibleName("Reintentar conexión")
            self.toggle_btn.setAccessibleName("Iniciar o detener transmisión")
            self.log_box.setAccessibleName("Registro de eventos")

        def _setup_tray(self):
            self.tray = None
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self.tray = QSystemTrayIcon(make_tray_icon(self._status_color), self)
            self.tray.setToolTip(f"USB Audio Stream v{APP_VERSION}")

            menu = QMenu()
            act_show = QAction("Mostrar / Ocultar ventana", self)
            act_show.triggered.connect(self._toggle_visibility)

            act_retry = QAction("Reintentar conexión", self)
            act_retry.triggered.connect(self._on_retry)

            act_toggle = QAction("Iniciar / Detener", self)
            act_toggle.triggered.connect(self._on_toggle)

            act_quit = QAction("Salir completamente", self)
            act_quit.triggered.connect(self._quit_app)

            menu.addAction(act_show)
            menu.addSeparator()
            menu.addAction(act_retry)
            menu.addAction(act_toggle)
            menu.addSeparator()
            menu.addAction(act_quit)

            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._on_tray_activated)
            self.tray.show()

        def _on_tray_activated(self, reason):
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self._toggle_visibility()

        def _toggle_visibility(self):
            if self.isVisible():
                self.hide()
            else:
                self.showNormal()
                self.raise_()
                self.activateWindow()

        def _quit_app(self):
            self._force_quit = True
            self._save_settings()
            self.controller.stop()
            if self.tray:
                self.tray.hide()
            self.app.quit()

        @staticmethod
        def _repolish(widget):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

        def _refresh_adb_status(self):
            """Lanza la comprobación de ADB en segundo plano para no bloquear la UI."""
            self.adb_badge.setText("ADB: comprobando...")
            self.adb_badge.setObjectName("adbNeutral")
            self.adb_label.setText("Verificando si adb está instalado y responde...")
            self._repolish(self.adb_badge)
            threading.Thread(target=self._adb_check_worker, daemon=True).start()

        def _adb_check_worker(self):
            state, detail = check_adb_health()
            self.bridge.tools_sig.emit(state, detail)

        def _update_adb_status(self, state, detail):
            if state == "ok":
                self.adb_badge.setText("ADB OK")
                self.adb_badge.setObjectName("adbOk")
            elif state == "missing":
                self.adb_badge.setText("ADB NO INSTALADO")
                self.adb_badge.setObjectName("adbError")
            else:
                self.adb_badge.setText("ADB CON ERROR")
                self.adb_badge.setObjectName("adbWarn")
            self._repolish(self.adb_badge)
            self.adb_label.setText(detail)
            self._append_log(f"[adb] {detail}")

        def _check_updates(self):
            """Consulta GitHub Releases en segundo plano."""
            self.update_label.setText("Buscando actualizaciones...")
            self.update_btn.setVisible(False)
            self.update_check_btn.setEnabled(False)
            self.update_check_btn.setText("Buscando...")
            threading.Thread(target=self._update_check_worker, daemon=True).start()

        def _update_check_worker(self):
            result = check_for_update(APP_VERSION)
            self.bridge.update_sig.emit(json.dumps(result))

        def _update_check_result(self, payload):
            try:
                result = json.loads(payload)
            except Exception:
                result = {"status": "error", "error": "respuesta inválida"}

            status = result.get("status")
            latest = result.get("latest") or ""
            error = result.get("error") or ""
            self._update_url = result.get("url") or RELEASES_URL
            self.update_check_btn.setEnabled(True)
            self.update_check_btn.setText("Buscar actualizaciones")

            if status == "update":
                self._latest_update = result
                self.update_label.setText(f"Actualización disponible: v{latest}")
                self.update_btn.setVisible(True)
                has_asset = bool(result.get("asset_url")) and can_self_update()
                self.update_btn.setText("Actualizar" if has_asset else "Descargar")
                self._append_log(
                    f"Actualización disponible: v{latest} — {self._update_url}"
                )
                if has_asset and latest and latest != self._auto_prompted_version:
                    self._auto_prompted_version = latest
                    self._open_update_window()
            elif status == "current":
                self.update_label.setText(f"Estás al día (v{APP_VERSION})")
                self.update_btn.setVisible(False)
                self._append_log(
                    f"Sin actualizaciones: v{APP_VERSION} es la última versión."
                )
            else:
                self.update_label.setText("No se pudo comprobar si hay actualizaciones.")
                self.update_btn.setVisible(False)
                self._append_log(
                    "No se pudo comprobar si hay actualizaciones"
                    + (f": {error}" if error else ".")
                )

        def _open_update_window(self):
            update = self._latest_update
            if not update or not update.get("asset_url") or not can_self_update():
                self._open_release_page()
                return
            if self._update_window is not None and self._update_window.isVisible():
                self._update_window.raise_()
                self._update_window.activateWindow()
                return
            if not self.isVisible():
                self.show()
            self._update_window = UpdateWindow(self, update, self._finish_update)
            self._update_window.show()
            self._update_window.raise_()
            self._update_window.activateWindow()

        def _finish_update(self):
            self._save_settings()
            self.controller.stop()
            if self.tray:
                self.tray.hide()
            self.app.quit()

        def _open_release_page(self):
            QDesktopServices.openUrl(QUrl(self._update_url))

        def _refresh_sources(self):
            current_data = self.source_combo.currentData()
            self.source_combo.blockSignals(True)
            self.source_combo.clear()
            for src_id, label in list_audio_sources():
                self.source_combo.addItem(label, src_id)
            if current_data:
                idx = self.source_combo.findData(current_data)
                if idx >= 0:
                    self.source_combo.setCurrentIndex(idx)
            self.source_combo.blockSignals(False)

        def _apply_saved_settings(self):
            settings = load_settings()

            gain_db = settings.get("gain_db")
            if isinstance(gain_db, (int, float)):
                value = int(round(max(-12.0, min(6.0, float(gain_db))) * 10))
                self.gain_slider.setValue(value)

            limiter = settings.get("soft_limiter")
            if isinstance(limiter, bool):
                self.limiter_cb.setChecked(limiter)

            profile = settings.get("profile")
            if profile:
                for index in range(self.profile_combo.count()):
                    data = self.profile_combo.itemData(index)
                    if data and data[0] == profile:
                        self.profile_combo.setCurrentIndex(index)
                        break

            capture = settings.get("capture_ms")
            chunk = settings.get("chunk_ms")
            if isinstance(capture, int) and isinstance(chunk, int):
                self.cap_spin.setValue(max(2, min(100, capture)))
                self.chk_spin.setValue(max(2, min(100, chunk)))
                self.controller.set_latency(
                    self.cap_spin.value(), self.chk_spin.value()
                )

            rate = settings.get("rate_mode")
            if rate:
                index = self.rate_combo.findData(rate)
                if index >= 0:
                    self.rate_combo.setCurrentIndex(index)

            source = settings.get("source_mode")
            if source:
                index = self.source_combo.findData(source)
                if index >= 0:
                    self.source_combo.setCurrentIndex(index)

        def _save_settings(self):
            if not self._settings_ready:
                return
            profile_data = self.profile_combo.currentData()
            save_settings(
                {
                    "profile": profile_data[0] if profile_data else "",
                    "capture_ms": self.cap_spin.value(),
                    "chunk_ms": self.chk_spin.value(),
                    "source_mode": self.source_combo.currentData() or "__default__",
                    "rate_mode": self.rate_combo.currentData() or "native",
                    "gain_db": self.controller.gain_db,
                    "soft_limiter": self.limiter_cb.isChecked(),
                    "theme": self._theme,
                }
            )

        def _on_source_changed(self, idx):
            mode = self.source_combo.itemData(idx)
            if mode:
                self.controller.set_source_mode(mode)
                self._save_settings()

        def _on_rate_changed(self, idx):
            mode = self.rate_combo.itemData(idx)
            if mode:
                self.controller.set_rate_mode(mode)
                self._save_settings()

        def _on_gain_changed(self, val):
            db = val / 10.0
            self.gain_val_lbl.setText(f"{db:+.1f} dB")
            self.controller.set_gain_db(db)
            self._save_settings()

        def _on_limiter_toggled(self, enabled):
            self.controller.set_soft_limiter(enabled)
            self._save_settings()

        def _on_profile_changed(self, idx):
            vals = self.profile_combo.itemData(idx)
            if vals:
                _key, cap_ms, chk_ms = vals
                self.cap_spin.setValue(cap_ms)
                self.chk_spin.setValue(chk_ms)
                self.controller.set_latency(cap_ms, chk_ms)
                self._save_settings()

        def _on_apply_latency(self):
            self.controller.set_latency(self.cap_spin.value(), self.chk_spin.value())
            self._save_settings()

        def _refresh_tray_icon(self):
            if not self.tray:
                return
            color = "#ef4444" if self._is_clipping else self._status_color
            self.tray.setIcon(make_tray_icon(color))

        def _update_device(self, state, text):
            self.device_label.setText(text)

        def _update_status(self, code, text):
            colors = {
                "streaming": "#22c55e",
                "connecting": "#3b82f6",
                "waiting_usb": "#eab308",
                "unauthorized": "#f97316",
                "retrying": "#f59e0b",
                "error": "#ef4444",
                "stopped": "#6b7280",
            }
            self._status_color = colors.get(code, "#9ca3af")
            self.dot.setStyleSheet(f"color: {self._status_color}; font-size: 15px;")
            self.status_label.setText(text)
            if self.tray:
                self.tray.setToolTip(f"USB Audio Stream v{APP_VERSION} — {text}")
            self._refresh_tray_icon()

        def _ui_rendering(self):
            """True solo si la ventana está visible, restaurada y en primer plano."""
            return (
                self.isVisible()
                and not self.isMinimized()
                and self.isActiveWindow()
            )

        def _update_level(self, level, clipping):
            clipping = bool(clipping)
            self._pending_level = float(level)
            if clipping != self._is_clipping:
                self._is_clipping = clipping
                self._refresh_tray_icon()
            if not self._ui_rendering():
                return
            self._render_level()

        def _render_level(self):
            self.level_bar.setValue(int(self._pending_level * 100))
            if self._badge_state != self._is_clipping:
                self._badge_state = self._is_clipping
                self._apply_clip_badge()

        def _apply_clip_badge(self):
            if self._is_clipping:
                self.clip_badge.setText("SATURACIÓN (CLIP)")
                self.clip_badge.setObjectName("clipAlert")
                self.level_bar.setObjectName("vuClip")
            else:
                self.clip_badge.setText("SEÑAL OK")
                self.clip_badge.setObjectName("clipOk")
                self.level_bar.setObjectName("vuNormal")
            self.clip_badge.style().unpolish(self.clip_badge)
            self.clip_badge.style().polish(self.clip_badge)
            self.level_bar.style().unpolish(self.level_bar)
            self.level_bar.style().polish(self.level_bar)

        def _refresh_rendered_ui(self):
            if self._ui_rendering():
                self._render_level()

        def showEvent(self, event):
            super().showEvent(event)
            QTimer.singleShot(0, self._refresh_rendered_ui)

        def changeEvent(self, event):
            super().changeEvent(event)
            if event.type() in (
                QEvent.Type.ActivationChange,
                QEvent.Type.WindowStateChange,
            ):
                QTimer.singleShot(0, self._refresh_rendered_ui)

        def _append_log(self, msg):
            ts = time.strftime("%H:%M:%S")
            self.log_box.appendPlainText(f"[{ts}] {msg}")
            log(msg)

        def _open_logs(self):
            if not open_log_folder():
                self._append_log("No se pudo abrir la carpeta de logs.")

        def _on_retry(self):
            self.toggle_btn.setText("Detener")
            self.controller.retry_now()

        def _on_toggle(self):
            if self.controller._running:
                self.controller.stop()
                self.toggle_btn.setText("Iniciar")
            else:
                self.controller.start()
                self.toggle_btn.setText("Detener")

        def closeEvent(self, event):
            self._save_settings()
            if not self._force_quit and self.tray and self.tray.isVisible():
                event.ignore()
                self.hide()
                self.tray.showMessage(
                    "USB Audio Stream",
                    "La aplicación sigue ejecutándose en la bandeja del sistema.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
            else:
                self.controller.stop()
                event.accept()

        def _apply_style(self):
            palette = get_theme(self._theme)
            self.setStyleSheet(Template("""
                QWidget {
                    background-color: $bg;
                    color: $text;
                    font-family: sans-serif;
                    font-size: 13px;
                }
                QScrollArea {
                    background-color: $bg;
                    border: none;
                }
                QLabel#titleLabel {
                    font-size: 18px;
                    font-weight: bold;
                    color: $title;
                }
                QLabel#versionLabel {
                    color: $section;
                    font-size: 11px;
                    font-weight: 600;
                }
                QLabel#subtitle {
                    color: $muted;
                    font-size: 12px;
                }
                QFrame#card {
                    background-color: $panel;
                    border: 1px solid $border;
                    border-radius: 10px;
                }
                QFrame#sep {
                    color: $border;
                }
                QLabel#sectionLabel {
                    color: $section;
                    font-size: 10px;
                    font-weight: bold;
                    letter-spacing: 0.6px;
                }
                QLabel#valueLabel {
                    font-size: 14px;
                    font-weight: 600;
                    color: $value;
                }
                QLabel#clipOk {
                    background-color: $ok_bg;
                    color: $ok_fg;
                    border: 1px solid $ok_border;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QLabel#clipAlert {
                    background-color: $error_bg;
                    color: $error_fg;
                    border: 1px solid $error_border;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QLabel#hintLabel {
                    color: $muted;
                    font-size: 11px;
                }
                QLabel#adbOk {
                    background-color: $ok_bg;
                    color: $ok_fg;
                    border: 1px solid $ok_border;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QLabel#adbWarn {
                    background-color: $warn_bg;
                    color: $warn_fg;
                    border: 1px solid $warn_border;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QLabel#adbError {
                    background-color: $error_bg;
                    color: $error_fg;
                    border: 1px solid $error_border;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QLabel#adbNeutral {
                    background-color: $neutral_bg;
                    color: $neutral_text;
                    border: 1px solid $neutral_border;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QProgressBar {
                    background-color: $progress_bg;
                    border: 1px solid $border;
                    border-radius: 5px;
                }
                QProgressBar#vuNormal::chunk {
                    background-color: #22c55e;
                    border-radius: 4px;
                }
                QProgressBar#vuClip::chunk {
                    background-color: #ef4444;
                    border-radius: 4px;
                }
                QComboBox, QSpinBox {
                    background-color: $input_bg;
                    border: 1px solid $input_border;
                    border-radius: 6px;
                    padding: 5px 8px;
                    color: $input_text;
                    font-size: 12px;
                }
                QCheckBox {
                    color: $neutral_text;
                    spacing: 6px;
                }
                QPushButton {
                    background-color: $button_bg;
                    border: 1px solid $button_border;
                    border-radius: 8px;
                    padding: 8px 14px;
                    font-weight: 600;
                    color: $button_text;
                }
                QPushButton:hover {
                    background-color: $button_hover;
                }
                QPushButton#primaryBtn {
                    background-color: $accent;
                    border: 1px solid $accent_border;
                    color: #ffffff;
                }
                QPushButton#primaryBtn:hover {
                    background-color: $accent_hover;
                }
                QPushButton#miniBtn {
                    padding: 2px 8px;
                    font-size: 11px;
                    border-radius: 5px;
                }
                QPlainTextEdit {
                    background-color: $log_bg;
                    border: 1px solid $log_border;
                    border-radius: 8px;
                    color: $log_text;
                    font-family: monospace;
                    font-size: 11px;
                    padding: 6px;
                }
            """).substitute(palette))
            self._update_theme_button()

        def _update_theme_button(self):
            if self._theme == "light":
                self.theme_btn.setText("🌙 Oscuro")
                self.theme_btn.setToolTip("Cambiar al tema oscuro")
                self.theme_btn.setAccessibleDescription("Cambiar al tema oscuro")
            else:
                self.theme_btn.setText("☀ Claro")
                self.theme_btn.setToolTip("Cambiar al tema claro")
                self.theme_btn.setAccessibleDescription("Cambiar al tema claro")

        def _toggle_theme(self):
            self._theme = "light" if self._theme == "dark" else "dark"
            self._apply_style()
            self._save_settings()

    class UpdateWindow(QDialog):
        progress_sig = Signal(int, int)
        status_sig = Signal(str)
        result_sig = Signal(bool, str, str)

        def __init__(self, parent, update, on_quit):
            super().__init__(parent)
            self.update = update
            self.on_quit = on_quit
            self.cancel_event = threading.Event()
            self.workdir = ""
            self.folder = ""

            self.setWindowTitle(f"Actualización v{update.get('latest', '')}")
            self.setWindowFlag(Qt.WindowType.Window, True)
            self.setMinimumWidth(480)
            palette = get_theme(getattr(parent, "_theme", "dark"))
            self.setStyleSheet(Template("""
                QDialog { background-color: $bg; }
                QLabel { color: $text; font-size: 12px; }
                QLabel#titleLabel { font-size: 17px; font-weight: bold; color: $title; }
                QProgressBar { background-color: $progress_bg; border: 1px solid $border;
                               border-radius: 5px; }
                QProgressBar::chunk { background-color: $accent; border-radius: 4px; }
                QPushButton { background-color: $button_bg; border: 1px solid $button_border;
                              border-radius: 8px; padding: 7px 14px; color: $button_text;
                              font-weight: 600; }
                QPushButton:hover { background-color: $button_hover; }
            """).substitute(palette))

            layout = QVBoxLayout(self)
            layout.setContentsMargins(20, 18, 20, 16)
            layout.setSpacing(10)

            title = QLabel(f"Actualizando a v{update.get('latest', '')}")
            title.setObjectName("titleLabel")
            self.info = QLabel("Preparando descarga...")
            self.info.setWordWrap(True)
            self.progress = QProgressBar()
            self.progress.setRange(0, 1000)
            self.progress.setTextVisible(False)
            self.progress.setFixedHeight(16)
            self.detail = QLabel("")
            self.detail.setWordWrap(True)
            self.detail.setStyleSheet(f"color: {palette['muted']}; font-size: 11px;")

            buttons = QHBoxLayout()
            self.folder_btn = QPushButton("Abrir carpeta")
            self.folder_btn.setVisible(False)
            self.folder_btn.clicked.connect(self._open_folder)
            self.cancel_btn = QPushButton("Cancelar")
            self.cancel_btn.clicked.connect(self._cancel)
            self.close_btn = QPushButton("Cerrar")
            self.close_btn.setVisible(False)
            self.close_btn.clicked.connect(self.close)
            buttons.addWidget(self.folder_btn)
            buttons.addStretch(1)
            buttons.addWidget(self.cancel_btn)
            buttons.addWidget(self.close_btn)

            self.setAccessibleName("Ventana de actualización")
            self.info.setAccessibleName("Estado de la actualización")
            self.progress.setAccessibleName("Progreso de actualización")
            self.detail.setAccessibleName("Detalle de la actualización")
            self.folder_btn.setAccessibleName("Abrir carpeta de descarga")
            self.cancel_btn.setAccessibleName("Cancelar actualización")
            self.close_btn.setAccessibleName("Cerrar")

            layout.addWidget(title)
            layout.addWidget(self.info)
            layout.addWidget(self.progress)
            layout.addWidget(self.detail)
            layout.addLayout(buttons)

            self.progress_sig.connect(self._on_progress)
            self.status_sig.connect(self._on_status)
            self.result_sig.connect(self._on_result)
            QTimer.singleShot(100, self.start_download)

        def start_download(self):
            asset = self.update.get("asset_name") or "paquete"
            self.status_sig.emit(f"Descargando {asset}...")
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            try:
                path, workdir = download_update(
                    self.update, self.progress_sig.emit, self.cancel_event
                )
                self.workdir = workdir
                if self.cancel_event.is_set():
                    raise UpdateCancelled()
                self.status_sig.emit("Verificando y aplicando la actualización...")
                result = apply_update(path, self.update.get("asset_name") or "")
                self.result_sig.emit(result.ok, result.message, result.folder or "")
            except UpdateCancelled:
                self.result_sig.emit(False, "Actualización cancelada.", "")
            except Exception as exc:
                self.result_sig.emit(False, f"No se pudo actualizar: {exc}", self.workdir)

        def _on_progress(self, written, total):
            if total > 0:
                if self.progress.maximum() != 1000:
                    self.progress.setRange(0, 1000)
                self.progress.setValue(int(written * 1000 / total))
                self.detail.setText(
                    f"{written / 1048576:.1f} / {total / 1048576:.1f} MB"
                )
            else:
                self.progress.setRange(0, 0)
                self.detail.setText(f"{written / 1048576:.1f} MB")

        def _on_status(self, text):
            self.info.setText(text)

        def _on_result(self, ok, message, folder):
            self.cancel_btn.setVisible(False)
            self.close_btn.setVisible(True)
            self.info.setText(message)
            self.folder = folder
            if ok:
                self.progress.setRange(0, 1000)
                self.progress.setValue(1000)
                self.detail.setText("La aplicación se reiniciará con la versión nueva.")
                QTimer.singleShot(1200, self.on_quit)
            elif folder:
                self.folder_btn.setVisible(True)
                self.detail.setText(f"Carpeta: {folder}")

        def _open_folder(self):
            if self.folder:
                QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder))

        def _cancel(self):
            self.cancel_event.set()
            self.status_sig.emit("Cancelando...")
            self.close()

        def closeEvent(self, event):
            self.cancel_event.set()
            super().closeEvent(event)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow(app)
    win.show()
    sys.exit(app.exec())


def run_tk_app():
    import tkinter as tk
    from tkinter import ttk, scrolledtext

    root = tk.Tk()
    root.title(f"USB Audio Stream v{APP_VERSION}")
    root.geometry("540x600")
    root.minsize(420, 440)
    root.configure(bg="#16181d")

    icon_path = get_icon_path()
    if icon_path:
        try:
            _app_icon = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, _app_icon)
            root._app_icon = _app_icon
        except Exception:
            pass

    root.columnconfigure(0, weight=1)
    root.rowconfigure(5, weight=1)

    hdr = tk.Frame(root, bg="#16181d")
    hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
    tk.Label(hdr, text=f"USB Audio Stream v{APP_VERSION}", bg="#16181d", fg="#f3f4f6", font=("Sans", 14, "bold")).pack(anchor="w")
    tk.Label(hdr, text="Audio de PipeWire hacia Android por cable USB (ADB)", bg="#16181d", fg="#9aa0ae", font=("Sans", 9)).pack(anchor="w")

    update_var = tk.StringVar(value="Buscando actualizaciones...")
    update_row = tk.Frame(hdr, bg="#16181d")
    update_row.pack(fill="x", pady=(4, 0))
    update_lbl = tk.Label(update_row, textvariable=update_var, bg="#16181d", fg="#9aa0ae", font=("Sans", 8), justify="left", anchor="w")
    update_lbl.pack(side="left", fill="x", expand=True)
    update_url_holder = {"url": RELEASES_URL}
    latest_update = {"data": None}
    auto_prompted = {"version": ""}
    update_window_holder = {"win": None}

    def open_update_url():
        webbrowser.open(update_url_holder["url"])

    def request_update_action():
        update = latest_update["data"]
        if update and update.get("asset_url") and can_self_update():
            open_update_window(update)
        else:
            open_update_url()

    update_btn = tk.Button(
        update_row, text="Descargar", command=lambda: request_update_action(),
        bg="#2563eb", fg="#ffffff", relief="flat", font=("Sans", 8, "bold"), padx=8,
    )
    check_update_btn = tk.Button(
        update_row, text="Buscar actualizaciones", command=lambda: check_updates_async(),
        bg="#282d37", fg="#e6e8ee", relief="flat", font=("Sans", 8, "bold"), padx=8,
    )
    check_update_btn.pack(side="right")

    card = tk.Frame(root, bg="#1f232b", highlightbackground="#2e3440", highlightthickness=1)
    card.grid(row=1, column=0, sticky="ew", padx=18, pady=4)

    tk.Label(card, text="DISPOSITIVO CONECTADO", bg="#1f232b", fg="#8b93a5", font=("Sans", 8, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
    dev_var = tk.StringVar(value="Buscando dispositivo...")
    dev_lbl = tk.Label(card, textvariable=dev_var, bg="#1f232b", fg="#f3f4f6", font=("Sans", 10, "bold"), justify="left")
    dev_lbl.pack(anchor="w", padx=14, pady=(0, 8), fill="x")

    tk.Frame(card, bg="#2e3440", height=1).pack(fill="x", padx=14)

    tk.Label(card, text="ESTADO DE CONEXIÓN", bg="#1f232b", fg="#8b93a5", font=("Sans", 8, "bold")).pack(anchor="w", padx=14, pady=(8, 2))
    st_row = tk.Frame(card, bg="#1f232b")
    st_row.pack(fill="x", padx=14, pady=(0, 8))

    dot_lbl = tk.Label(st_row, text="●", bg="#1f232b", fg="#eab308", font=("Sans", 12))
    dot_lbl.pack(side="left", padx=(0, 8))

    status_var = tk.StringVar(value="Iniciando...")
    st_lbl = tk.Label(st_row, textvariable=status_var, bg="#1f232b", fg="#f3f4f6", font=("Sans", 10, "bold"), justify="left")
    st_lbl.pack(side="left", fill="x", expand=True)

    tk.Frame(card, bg="#2e3440", height=1).pack(fill="x", padx=14)

    adb_hdr = tk.Frame(card, bg="#1f232b")
    adb_hdr.pack(fill="x", padx=14, pady=(8, 2))
    tk.Label(adb_hdr, text="ADB (DEPURACIÓN USB)", bg="#1f232b", fg="#8b93a5", font=("Sans", 8, "bold")).pack(side="left")

    adb_var = tk.StringVar(value="ADB: comprobando...")
    adb_lbl = tk.Label(adb_hdr, textvariable=adb_var, bg="#1f232b", fg="#e6e8ee", font=("Sans", 8, "bold"))
    adb_lbl.pack(side="right")

    adb_btn = tk.Button(adb_hdr, text="↻", bg="#282d37", fg="#e6e8ee", relief="flat", font=("Sans", 8, "bold"), padx=4)
    adb_btn.pack(side="right", padx=(0, 8))

    adb_detail_var = tk.StringVar(value="Verificando si adb está instalado y responde...")
    adb_detail_lbl = tk.Label(card, textvariable=adb_detail_var, bg="#1f232b", fg="#9aa0ae", font=("Sans", 8), justify="left", anchor="w")
    adb_detail_lbl.pack(anchor="w", padx=14, pady=(0, 8), fill="x")

    tk.Frame(card, bg="#2e3440", height=1).pack(fill="x", padx=14)

    lvl_hdr = tk.Frame(card, bg="#1f232b")
    lvl_hdr.pack(fill="x", padx=14, pady=(8, 4))
    tk.Label(lvl_hdr, text="NIVEL DE SEÑAL", bg="#1f232b", fg="#8b93a5", font=("Sans", 8, "bold")).pack(side="left")
    clip_lbl = tk.Label(lvl_hdr, text="SEÑAL OK", bg="#193226", fg="#4ade80", font=("Sans", 8, "bold"), padx=6, pady=1)
    clip_lbl.pack(side="right")

    vu_canvas = tk.Canvas(card, height=10, bg="#111317", highlightthickness=0)
    vu_canvas.pack(fill="x", padx=14, pady=(0, 12))
    vu_rect = vu_canvas.create_rectangle(0, 0, 0, 10, fill="#22c55e", width=0)

    cfg = tk.Frame(root, bg="#1f232b", highlightbackground="#2e3440", highlightthickness=1)
    cfg.grid(row=2, column=0, sticky="ew", padx=18, pady=6)

    tk.Label(cfg, text="FUENTE DE AUDIO Y FRECUENCIA", bg="#1f232b", fg="#8b93a5", font=("Sans", 8, "bold")).pack(anchor="w", padx=14, pady=(8, 4))
    src_row = tk.Frame(cfg, bg="#1f232b")
    src_row.pack(fill="x", padx=14, pady=(0, 8))

    sources_map = {}
    src_combo = ttk.Combobox(src_row, state="readonly")
    src_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))

    rate_combo = ttk.Combobox(src_row, state="readonly", width=12, values=["Nativa", "44100", "48000", "96000"])
    rate_combo.current(0)
    rate_combo.pack(side="right")

    gain_row = tk.Frame(cfg, bg="#1f232b")
    gain_row.pack(fill="x", padx=14, pady=(0, 8))
    tk.Label(gain_row, text="Ganancia:", bg="#1f232b", fg="#cfd5e2").pack(side="left")
    gain_var = tk.DoubleVar(value=0.0)
    lim_var = tk.BooleanVar(value=True)

    profile_row = tk.Frame(cfg, bg="#1f232b")
    profile_row.pack(fill="x", padx=14, pady=(0, 8))
    tk.Label(profile_row, text="Perfil:", bg="#1f232b", fg="#cfd5e2").pack(side="left")
    profile_map = {
        "Baja latencia (8 ms)": (8, 8),
        "Equilibrado (15 ms)": (15, 15),
        "Alta estabilidad (30 ms)": (30, 30),
    }
    profile_combo = ttk.Combobox(profile_row, state="readonly", values=list(profile_map.keys()))
    profile_combo.current(1)
    profile_combo.pack(side="left", fill="x", expand=True, padx=(8, 0))

    lat_frame = tk.Frame(cfg, bg="#1f232b")
    lat_frame.pack(fill="x", padx=14, pady=(0, 10))
    tk.Label(lat_frame, text="Captura:", bg="#1f232b", fg="#cfd5e2").pack(side="left")
    cap_spin = tk.Spinbox(lat_frame, from_=2, to=100, width=4)
    cap_spin.delete(0, "end")
    cap_spin.insert(0, str(DEFAULT_CAPTURE_MS))
    cap_spin.pack(side="left", padx=(4, 10))

    tk.Label(lat_frame, text="Paquete:", bg="#1f232b", fg="#cfd5e2").pack(side="left")
    chk_spin = tk.Spinbox(lat_frame, from_=2, to=100, width=4)
    chk_spin.delete(0, "end")
    chk_spin.insert(0, str(DEFAULT_CHUNK_MS))
    chk_spin.pack(side="left", padx=(4, 10))

    log_lines = deque(maxlen=MAX_LOG_LINES)
    log_hdr = tk.Frame(root, bg="#16181d")
    log_hdr.grid(row=4, column=0, sticky="ew", padx=18, pady=(4, 2))
    tk.Label(log_hdr, text="LOG EN VIVO", bg="#16181d", fg="#8b93a5", font=("Sans", 8, "bold")).pack(side="left")

    def open_logs_tk():
        if not open_log_folder():
            append_log("No se pudo abrir la carpeta de logs.")

    tk.Button(
        log_hdr, text="Abrir carpeta", command=open_logs_tk, bg="#282d37",
        fg="#e6e8ee", relief="flat", font=("Sans", 8, "bold"), padx=8,
    ).pack(side="right")

    log_box = scrolledtext.ScrolledText(root, height=6, bg="#111317", fg="#9ca3af", font=("Monospace", 8), borderwidth=1, relief="flat")
    log_box.grid(row=5, column=0, sticky="nsew", padx=18, pady=(0, 14))

    def append_log(msg):
        ts = time.strftime("%H:%M:%S")
        log_lines.append(f"[{ts}] {msg}")
        log(msg)
        def _sync():
            log_box.delete("1.0", "end")
            log_box.insert("end", "\n".join(log_lines) + "\n")
            log_box.see("end")
        root.after(0, _sync)

    original_report_exception = root.report_callback_exception

    def report_callback_exception(exc_type, exc_value, exc_tb):
        log_exception(exc_type, exc_value, exc_tb, "Excepción no controlada en la UI (Tk)")
        original_report_exception(exc_type, exc_value, exc_tb)

    root.report_callback_exception = report_callback_exception

    def update_device(state, text):
        root.after(0, lambda: dev_var.set(text))

    def update_status(code, text):
        colors = {
            "streaming": "#22c55e",
            "connecting": "#3b82f6",
            "waiting_usb": "#eab308",
            "unauthorized": "#f97316",
            "retrying": "#f59e0b",
            "error": "#ef4444",
            "stopped": "#6b7280",
        }
        c = colors.get(code, "#9ca3af")
        root.after(0, lambda: (dot_lbl.config(fg=c), status_var.set(text)))

    def update_adb_status(state, detail):
        styles = {
            "ok": ("ADB OK", "#4ade80"),
            "missing": ("ADB NO INSTALADO", "#ef4444"),
            "error": ("ADB CON ERROR", "#f59e0b"),
        }
        label_text, color = styles.get(state, ("ADB: ?", "#9ca3af"))
        root.after(0, lambda: (
            adb_var.set(label_text),
            adb_lbl.config(fg=color),
            adb_detail_var.set(detail),
        ))

    def check_adb_async():
        adb_var.set("ADB: comprobando...")
        adb_lbl.config(fg="#e6e8ee")
        adb_detail_var.set("Verificando si adb está instalado y responde...")

        def _work():
            state, detail = check_adb_health()
            update_adb_status(state, detail)
            append_log(f"[adb] {detail}")

        threading.Thread(target=_work, daemon=True).start()

    adb_btn.config(command=check_adb_async)

    def check_updates_async():
        check_update_btn.config(state="disabled", text="Buscando...")

        def _work():
            result = check_for_update(APP_VERSION)

            def _apply():
                check_update_btn.config(state="normal", text="Buscar actualizaciones")
                status = result["status"]
                if status == "update":
                    latest = result.get("latest") or ""
                    latest_update["data"] = result
                    update_url_holder["url"] = result.get("url") or RELEASES_URL
                    update_var.set(f"Actualización disponible: v{latest}")
                    has_asset = bool(result.get("asset_url")) and can_self_update()
                    update_btn.config(text="Actualizar" if has_asset else "Descargar")
                    update_btn.pack(side="right", padx=(8, 0))
                    append_log(f"Actualización disponible: v{latest} — {update_url_holder['url']}")
                    if (has_asset and latest
                            and latest != auto_prompted["version"]):
                        auto_prompted["version"] = latest
                        open_update_window(result)
                elif status == "current":
                    update_var.set(f"Estás al día (v{APP_VERSION})")
                    update_btn.pack_forget()
                    append_log(f"Sin actualizaciones: v{APP_VERSION} es la última versión.")
                else:
                    update_var.set("No se pudo comprobar si hay actualizaciones.")
                    update_btn.pack_forget()
                    error = result.get("error") or "sin conexión"
                    append_log(f"No se pudo comprobar si hay actualizaciones: {error}")

            root.after(0, _apply)

        threading.Thread(target=_work, daemon=True).start()

    def update_level(level, clipping):
        def _draw():
            w = vu_canvas.winfo_width() or 440
            vu_canvas.coords(vu_rect, 0, 0, int(w * level), 10)
            if clipping:
                vu_canvas.itemconfig(vu_rect, fill="#ef4444")
                clip_lbl.config(text="SATURACIÓN (CLIP)", bg="#451a1a", fg="#fca5a5")
            else:
                vu_canvas.itemconfig(vu_rect, fill="#22c55e")
                clip_lbl.config(text="SEÑAL OK", bg="#193226", fg="#4ade80")
        root.after(0, _draw)

    ctrl = StreamController(update_status, update_device, append_log, update_level)
    settings_ready = {"value": False}

    def save_settings_tk():
        if not settings_ready["value"]:
            return
        try:
            capture = int(cap_spin.get())
            chunk = int(chk_spin.get())
            gain = float(gain_var.get())
        except (ValueError, tk.TclError):
            return
        save_settings(
            {
                "profile": profile_combo.get(),
                "capture_ms": capture,
                "chunk_ms": chunk,
                "source_mode": sources_map.get(src_combo.get(), "__default__"),
                "rate_mode": "native" if rate_combo.get() == "Nativa" else rate_combo.get(),
                "gain_db": gain,
                "soft_limiter": bool(lim_var.get()),
            }
        )

    def apply_settings_tk():
        settings = load_settings()

        gain = settings.get("gain_db")
        if isinstance(gain, (int, float)):
            gain = max(-12.0, min(6.0, float(gain)))
            gain_var.set(gain)
            ctrl.set_gain_db(gain)

        limiter = settings.get("soft_limiter")
        if isinstance(limiter, bool):
            lim_var.set(limiter)
            ctrl.set_soft_limiter(limiter)

        profile = settings.get("profile")
        if profile in profile_map:
            profile_combo.set(profile)
            cap_ms, chk_ms = profile_map[profile]
            cap_spin.delete(0, "end")
            cap_spin.insert(0, str(cap_ms))
            chk_spin.delete(0, "end")
            chk_spin.insert(0, str(chk_ms))
            ctrl.set_latency(cap_ms, chk_ms)

        rate_mode = settings.get("rate_mode")
        rate_label = "Nativa" if rate_mode == "native" else rate_mode
        if rate_label in rate_combo["values"]:
            rate_combo.set(rate_label)
            ctrl.set_rate_mode(rate_mode)

        source_mode = settings.get("source_mode")
        if source_mode:
            for label, source_id in sources_map.items():
                if source_id == source_mode:
                    src_combo.set(label)
                    ctrl.set_source_mode(source_mode)
                    break

        capture = settings.get("capture_ms")
        chunk = settings.get("chunk_ms")
        if isinstance(capture, int) and isinstance(chunk, int):
            cap_spin.delete(0, "end")
            cap_spin.insert(0, str(max(2, min(100, capture))))
            chk_spin.delete(0, "end")
            chk_spin.insert(0, str(max(2, min(100, chunk))))
            ctrl.set_latency(cap_spin.get(), chk_spin.get())

        settings_ready["value"] = True

    def open_update_window(update):
        existing = update_window_holder["win"]
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(root)
        update_window_holder["win"] = win
        win.title(f"Actualización v{update.get('latest', '')}")
        win.configure(bg="#16181d")
        win.geometry("500x230")
        win.transient(root)
        cancel_event = threading.Event()
        folder_holder = {"folder": ""}
        progress_state = {"indeterminate": False}

        tk.Label(
            win, text=f"Actualizando a v{update.get('latest', '')}",
            bg="#16181d", fg="#f8fafc", font=("Sans", 13, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 6))
        info_var = tk.StringVar(value="Preparando descarga...")
        tk.Label(
            win, textvariable=info_var, bg="#16181d", fg="#e6e8ee",
            font=("Sans", 9), wraplength=460, justify="left", anchor="w",
        ).pack(fill="x", padx=18)
        progress = ttk.Progressbar(win, orient="horizontal", mode="determinate", maximum=1000)
        progress.pack(fill="x", padx=18, pady=(10, 4))
        detail_var = tk.StringVar(value="")
        tk.Label(
            win, textvariable=detail_var, bg="#16181d", fg="#9aa0ae",
            font=("Sans", 8), wraplength=460, justify="left", anchor="w",
        ).pack(fill="x", padx=18)

        buttons = tk.Frame(win, bg="#16181d")
        buttons.pack(fill="x", padx=18, pady=(10, 12))

        def close_window():
            cancel_event.set()
            update_window_holder["win"] = None
            win.destroy()

        def open_folder():
            folder = folder_holder["folder"]
            if not folder:
                return
            try:
                if sys.platform.startswith("win"):
                    os.startfile(folder)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception:
                pass

        folder_btn = tk.Button(
            buttons, text="Abrir carpeta", command=open_folder,
            bg="#282d37", fg="#e6e8ee", relief="flat", font=("Sans", 8, "bold"), padx=8,
        )
        cancel_btn = tk.Button(
            buttons, text="Cancelar", command=close_window,
            bg="#282d37", fg="#e6e8ee", relief="flat", font=("Sans", 8, "bold"), padx=12,
        )
        cancel_btn.pack(side="right")

        def on_progress(written, total):
            if not win.winfo_exists():
                return
            if total > 0:
                if progress_state["indeterminate"]:
                    progress.stop()
                    progress_state["indeterminate"] = False
                progress.configure(mode="determinate", maximum=1000)
                progress["value"] = int(written * 1000 / total)
                detail_var.set(f"{written / 1048576:.1f} / {total / 1048576:.1f} MB")
            else:
                progress.configure(mode="indeterminate")
                progress.start(80)
                progress_state["indeterminate"] = True
                detail_var.set(f"{written / 1048576:.1f} MB")

        def finish(ok, message, folder):
            if not win.winfo_exists():
                return
            if progress_state["indeterminate"]:
                progress.stop()
                progress_state["indeterminate"] = False
            info_var.set(message)
            cancel_btn.config(text="Cerrar", command=close_window)
            if ok:
                progress.configure(mode="determinate", maximum=1000)
                progress["value"] = 1000
                detail_var.set("La aplicación se reiniciará con la versión nueva.")
                root.after(
                    1200,
                    lambda: (save_settings_tk(), ctrl.stop(), root.destroy()),
                )
            elif folder:
                folder_holder["folder"] = folder
                folder_btn.pack(side="left")
                detail_var.set(f"Carpeta: {folder}")

        def worker():
            try:
                path, _workdir = download_update(
                    update,
                    lambda written, total: root.after(
                        0, lambda: on_progress(written, total)
                    ),
                    cancel_event,
                )
                root.after(0, lambda: info_var.set("Verificando y aplicando..."))
                result = apply_update(path, update.get("asset_name") or "")
                root.after(0, lambda: finish(result.ok, result.message, result.folder))
            except UpdateCancelled:
                root.after(0, lambda: finish(False, "Actualización cancelada.", ""))
            except Exception as exc:
                message = f"No se pudo actualizar: {exc}"
                root.after(0, lambda: finish(False, message, ""))

        win.protocol("WM_DELETE_WINDOW", close_window)
        threading.Thread(target=worker, daemon=True).start()

    def on_gain_changed(value):
        ctrl.set_gain_db(float(value))
        save_settings_tk()

    tk.Scale(
        gain_row, from_=-12, to=6, resolution=0.5, orient="horizontal",
        variable=gain_var, showvalue=True, bg="#1f232b", fg="#f3f4f6",
        highlightthickness=0, command=on_gain_changed
    ).pack(side="left", fill="x", expand=True, padx=8)

    def on_limiter_changed():
        ctrl.set_soft_limiter(lim_var.get())
        save_settings_tk()

    tk.Checkbutton(
        gain_row, text="Limitador", variable=lim_var,
        bg="#1f232b", fg="#cfd5e2", selectcolor="#16181d",
        command=on_limiter_changed
    ).pack(side="right")

    opts = list_audio_sources()
    for sid, lbl in opts:
        sources_map[lbl] = sid
    src_combo["values"] = [lbl for _, lbl in opts]
    if opts:
        src_combo.current(0)

    def on_source_selected(_event):
        ctrl.set_source_mode(sources_map.get(src_combo.get(), "__default__"))
        save_settings_tk()

    def on_rate_selected(_event):
        mode = "native" if rate_combo.get() == "Nativa" else rate_combo.get()
        ctrl.set_rate_mode(mode)
        save_settings_tk()

    src_combo.bind("<<ComboboxSelected>>", on_source_selected)
    rate_combo.bind("<<ComboboxSelected>>", on_rate_selected)

    def on_profile_selected(_event):
        vals = profile_map.get(profile_combo.get())
        if vals:
            cap_spin.delete(0, "end")
            cap_spin.insert(0, str(vals[0]))
            chk_spin.delete(0, "end")
            chk_spin.insert(0, str(vals[1]))
            ctrl.set_latency(*vals)
            save_settings_tk()

    profile_combo.bind("<<ComboboxSelected>>", on_profile_selected)

    def on_apply_latency():
        ctrl.set_latency(cap_spin.get(), chk_spin.get())
        save_settings_tk()

    tk.Button(
        lat_frame, text="Aplicar", bg="#282d37", fg="#e6e8ee", relief="flat",
        command=on_apply_latency,
    ).pack(side="right")

    btn_row = tk.Frame(root, bg="#16181d")
    btn_row.grid(row=3, column=0, sticky="ew", padx=18, pady=4)

    toggle_btn = tk.Button(btn_row, text="Detener", bg="#282d37", fg="#e6e8ee", relief="flat", padx=14, pady=7, font=("Sans", 9, "bold"))

    def on_retry():
        toggle_btn.config(text="Detener")
        ctrl.retry_now()

    def on_toggle():
        if ctrl._running:
            ctrl.stop()
            toggle_btn.config(text="Iniciar")
        else:
            ctrl.start()
            toggle_btn.config(text="Detener")

    toggle_btn.config(command=on_toggle)
    tk.Button(btn_row, text="Reintentar conexión", command=on_retry, bg="#2563eb", fg="#ffffff",
              relief="flat", padx=16, pady=7, font=("Sans", 9, "bold")).pack(side="left", fill="x", expand=True, padx=(0, 8))
    toggle_btn.pack(side="right")

    def on_configure(event):
        if event.widget is root:
            wrap = max(260, event.width - 80)
            dev_lbl.configure(wraplength=wrap)
            st_lbl.configure(wraplength=wrap - 30)
            adb_detail_lbl.configure(wraplength=wrap - 60)
            update_lbl.configure(wraplength=wrap)

    root.bind("<Configure>", on_configure)

    apply_settings_tk()

    missing = check_dependencies()
    if missing:
        update_status("error", f"Faltan herramientas: {', '.join(missing)}")
        append_log("Instala: sudo pacman -S --needed android-tools libpulse pipewire-pulse")
    else:
        ctrl.start()

    check_adb_async()
    check_updates_async()

    def periodic_update_check():
        check_updates_async()
        root.after(6 * 60 * 60 * 1000, periodic_update_check)

    root.after(6 * 60 * 60 * 1000, periodic_update_check)

    def on_close():
        save_settings_tk()
        ctrl.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main():
    setup_logging()
    install_exception_hooks()
    log_startup_info()
    for binding in ("PySide6", "PyQt6"):
        try:
            __import__(binding)
            log(f"Interfaz Qt: {binding} {qt_binding_version(binding)}")
            run_qt_app(binding)
            return
        except ImportError:
            continue
    log("Interfaz Qt no disponible; usando la interfaz Tk.")
    run_tk_app()


if __name__ == "__main__":
    main()
