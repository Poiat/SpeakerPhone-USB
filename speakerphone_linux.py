#!/usr/bin/env python3
"""
USB Audio Stream GUI v4
Transmite el audio del PC (PipeWire/PulseAudio) a Android por cable USB.
Novedades v4:
- Mejoras de audio:
  * Control de Ganancia (-12 dB a +6 dB) y Limitador Suave (Soft Limiter) en tiempo real para evitar distorsión/clipping
  * Selector de frecuencia de muestreo (Nativa de PipeWire, 44100 Hz, 48000 Hz, 96000 Hz)
  * Perfiles de latencia/estabilidad (Baja latencia 8ms, Equilibrado 15ms, Música sin cortes 30ms) + ajuste manual
- Funcionalidad:
  * Reconexión silenciosa automática al suspender/reanudar (systemd-logind D-Bus / gdbus / dbus-monitor) o desbloquear pantalla
- Interfaz (UI):
  * Layout fluido y adaptable que se ajusta naturalmente al tamaño de la ventana sin escalar tipografías artificialmente
"""
from array import array
from collections import deque
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

PKG = "com.usbaudio"
DEFAULT_PORT = 5000
DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
DEFAULT_CAPTURE_MS = 15
DEFAULT_CHUNK_MS = 15
CLIP_THRESHOLD = 0.98
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
    """Consulta `adb devices -l` y devuelve (estado, descripcion_dispositivo)."""
    try:
        out = run_cmd(["adb", "devices", "-l"], timeout=3)
    except Exception as e:
        return "error", f"Error ejecutando adb: {e}"

    lines = [l.strip() for l in out.splitlines()[1:] if l.strip()]
    if not lines:
        return "none", "Ningún dispositivo conectado"

    line = lines[0]
    parts = line.split()
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

    if state == "device":
        return "ready", display
    elif state == "unauthorized":
        return "unauthorized", f"{display} — Pendiente de autorización USB"
    elif state == "offline":
        return "offline", f"{display} — Fuera de línea (offline)"
    else:
        return state, f"{display} ({state})"


def process_pcm_s16le(data, gain_linear, soft_limiter, compute_meter):
    """
    Aplica ganancia y limitador suave (tanh) al bloque PCM s16le si es necesario,
    y calcula (bytes_procesados, nivel_0_1, hay_saturacion).
    """
    usable = len(data) - (len(data) & 1)
    if usable <= 0:
        return data, 0.0, False

    need_dsp = (abs(gain_linear - 1.0) > 0.005) or soft_limiter

    if not need_dsp and not compute_meter:
        return data, 0.0, False

    samples = array("h")
    samples.frombytes(data[:usable])
    if sys.byteorder != "little":
        samples.byteswap()

    clipping = False
    if need_dsp:
        tanh = math.tanh
        g = gain_linear
        if soft_limiter:
            # Limitador suave tipo knee con compresión tanh por encima de 0.75
            for i, s in enumerate(samples):
                x = (s / 32768.0) * g
                ax = -x if x < 0.0 else x
                if ax > 0.75:
                    excess = (ax - 0.75) / 0.25
                    y = 0.75 + 0.245 * tanh(excess)
                    x = -y if x < 0.0 else y
                if x >= 0.99:
                    clipping = True
                samples[i] = int(max(-32767, min(32767, x * 32767.0)))
        else:
            for i, s in enumerate(samples):
                v = int(s * g)
                if v > 32767:
                    v = 32767
                    clipping = True
                elif v < -32768:
                    v = -32768
                    clipping = True
                samples[i] = v

        if sys.byteorder != "little":
            samples.byteswap()
        out_bytes = samples.tobytes()
    else:
        out_bytes = data[:usable]

    if not compute_meter:
        return out_bytes, 0.0, clipping

    sub = samples[::4] if len(samples) > 128 else samples
    if not sub:
        return out_bytes, 0.0, clipping

    peak = max(max(sub), -min(sub))
    sum_sq = sum(s * s for s in sub)
    rms = math.sqrt(sum_sq / len(sub)) / 32768.0
    level = min(1.0, math.pow(rms * 2.5, 0.65))
    if (peak / 32768.0) >= CLIP_THRESHOLD and not soft_limiter:
        clipping = True
    return out_bytes, level, clipping


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
        t1 = threading.Thread(target=self._watch_system_sleep, daemon=True)
        t2 = threading.Thread(target=self._watch_session_lock, daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()

    def stop(self):
        self._stop.set()
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        self._procs.clear()

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

    def start(self):
        if self._running:
            return
        self._running = True
        self._suspended = False
        self._stop_event.clear()
        self._retry_event.clear()

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
        self.on_level(0.0, False)
        self.on_status("stopped", "Desconectado (transmisión detenida)")
        self.on_log("Transmisión detenida.")

    def _handle_sleep_state(self, is_sleeping):
        self._suspended = is_sleeping
        if is_sleeping:
            self._abort_active_io()
            self._cleanup_virtual_sink()
            self._remove_adb_forward()
            self.on_level(0.0, False)
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
        self.on_level(0.0, False)
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
                self.on_level(0.0, False)
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
        sock.sendall(struct.pack(">IBB", self.rate, self.channels, 16))

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
                    self.on_level(float(lvl), bool(now < clip_hold_until))
                    last_ui_push = now
        finally:
            self._abort_active_io()
            self._cleanup_virtual_sink()
            self.on_level(0.0, False)


def run_qt_app(qt_binding):
    if qt_binding == "PySide6":
        from PySide6.QtCore import Qt, Signal, QObject
        from PySide6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor, QAction
        from PySide6.QtWidgets import (
            QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
            QSizePolicy, QLabel, QPushButton, QComboBox, QSpinBox, QSlider,
            QCheckBox, QFrame, QPlainTextEdit, QProgressBar, QSystemTrayIcon,
            QMenu, QScrollArea
        )
    else:
        from PyQt6.QtCore import Qt, pyqtSignal as Signal, QObject
        from PyQt6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor, QAction
        from PyQt6.QtWidgets import (
            QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
            QSizePolicy, QLabel, QPushButton, QComboBox, QSpinBox, QSlider,
            QCheckBox, QFrame, QPlainTextEdit, QProgressBar, QSystemTrayIcon,
            QMenu, QScrollArea
        )

    _icon_cache = {}

    def make_tray_icon(dot_color_hex):
        if dot_color_hex in _icon_cache:
            return _icon_cache[dot_color_hex]
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
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
        status_sig = Signal(str, str)
        device_sig = Signal(str, str)
        log_sig = Signal(str)
        level_sig = Signal(float, bool)

    class MainWindow(QWidget):
        def __init__(self, app):
            super().__init__()
            self.app = app
            self._force_quit = False
            self._status_color = "#eab308"
            self._is_clipping = False

            self.setWindowTitle("USB Audio Stream")
            self.setWindowIcon(make_tray_icon("#3b82f6"))
            self.setMinimumSize(420, 440)
            self.resize(540, 620)

            self.bridge = Bridge()
            self.bridge.status_sig.connect(self._update_status)
            self.bridge.device_sig.connect(self._update_device)
            self.bridge.log_sig.connect(self._append_log)
            self.bridge.level_sig.connect(self._update_level, Qt.ConnectionType.QueuedConnection)

            self.controller = StreamController(
                on_status=self.bridge.status_sig.emit,
                on_device=self.bridge.device_sig.emit,
                on_log=self.bridge.log_sig.emit,
                on_level=self.bridge.level_sig.emit,
            )

            self._build_ui()
            self._setup_tray()
            self._apply_style()
            self._refresh_sources()

            missing = check_dependencies()
            if missing:
                self._update_status("error", f"Faltan herramientas: {', '.join(missing)}")
                self._append_log(
                    "Instala las dependencias en CachyOS/Arch:\n"
                    "sudo pacman -S --needed android-tools libpulse pipewire-pulse"
                )
            else:
                self.controller.start()

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
            subtitle = QLabel("Audio de PipeWire hacia Android por cable USB (ADB) • Reconexión automática")
            subtitle.setObjectName("subtitle")
            subtitle.setWordWrap(True)

            root.addWidget(title)
            root.addWidget(subtitle)

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
            self.limiter_cb.toggled.connect(self.controller.set_soft_limiter)

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
            self.profile_combo.addItem("Equilibrado (15 ms)", (15, 15))
            self.profile_combo.addItem("Baja latencia (8 ms)", (8, 8))
            self.profile_combo.addItem("Alta estabilidad / Música (30 ms)", (30, 25))
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
            clear_log_btn = QPushButton("Limpiar")
            clear_log_btn.setObjectName("miniBtn")
            clear_log_btn.clicked.connect(lambda: self.log_box.clear())
            log_hdr.addWidget(log_lbl)
            log_hdr.addStretch(1)
            log_hdr.addWidget(clear_log_btn)
            root.addLayout(log_hdr)

            self.log_box = QPlainTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setMaximumBlockCount(MAX_LOG_LINES)
            self.log_box.setMinimumHeight(80)
            self.log_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.log_box.setPlaceholderText("Eventos de conexión y audio en tiempo real...")
            root.addWidget(self.log_box, 1)

        def _setup_tray(self):
            self.tray = None
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self.tray = QSystemTrayIcon(make_tray_icon(self._status_color), self)
            self.tray.setToolTip("USB Audio Stream")

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
                self.show()
                self.raise_()
                self.activateWindow()

        def _quit_app(self):
            self._force_quit = True
            self.controller.stop()
            if self.tray:
                self.tray.hide()
            self.app.quit()

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

        def _on_source_changed(self, idx):
            mode = self.source_combo.itemData(idx)
            if mode:
                self.controller.set_source_mode(mode)

        def _on_rate_changed(self, idx):
            mode = self.rate_combo.itemData(idx)
            if mode:
                self.controller.set_rate_mode(mode)

        def _on_gain_changed(self, val):
            db = val / 10.0
            self.gain_val_lbl.setText(f"{db:+.1f} dB")
            self.controller.set_gain_db(db)

        def _on_profile_changed(self, idx):
            vals = self.profile_combo.itemData(idx)
            if vals:
                cap_ms, chk_ms = vals
                self.cap_spin.setValue(cap_ms)
                self.chk_spin.setValue(chk_ms)
                self.controller.set_latency(cap_ms, chk_ms)

        def _on_apply_latency(self):
            self.controller.set_latency(self.cap_spin.value(), self.chk_spin.value())

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
                self.tray.setToolTip(f"USB Audio Stream — {text}")
            self._refresh_tray_icon()

        def _update_level(self, level, clipping):
            self.level_bar.setValue(int(level * 100))
            if clipping != self._is_clipping:
                self._is_clipping = clipping
                if clipping:
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
                self._refresh_tray_icon()

        def _append_log(self, msg):
            ts = time.strftime("%H:%M:%S")
            self.log_box.appendPlainText(f"[{ts}] {msg}")

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
            self.setStyleSheet("""
                QWidget {
                    background-color: #16181d;
                    color: #e6e8ee;
                    font-family: sans-serif;
                    font-size: 13px;
                }
                QScrollArea {
                    background-color: #16181d;
                    border: none;
                }
                QLabel#titleLabel {
                    font-size: 18px;
                    font-weight: bold;
                    color: #f8fafc;
                }
                QLabel#subtitle {
                    color: #9aa0ae;
                    font-size: 12px;
                }
                QFrame#card {
                    background-color: #1f232b;
                    border: 1px solid #2e3440;
                    border-radius: 10px;
                }
                QFrame#sep {
                    color: #2e3440;
                }
                QLabel#sectionLabel {
                    color: #8b93a5;
                    font-size: 10px;
                    font-weight: bold;
                    letter-spacing: 0.6px;
                }
                QLabel#valueLabel {
                    font-size: 14px;
                    font-weight: 600;
                    color: #f3f4f6;
                }
                QLabel#clipOk {
                    background-color: #193226;
                    color: #4ade80;
                    border: 1px solid #22543d;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QLabel#clipAlert {
                    background-color: #451a1a;
                    color: #fca5a5;
                    border: 1px solid #ef4444;
                    border-radius: 5px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }
                QProgressBar {
                    background-color: #111317;
                    border: 1px solid #2e3440;
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
                    background-color: #16181d;
                    border: 1px solid #3a4150;
                    border-radius: 6px;
                    padding: 5px 8px;
                    color: #f3f4f6;
                    font-size: 12px;
                }
                QCheckBox {
                    color: #cfd5e2;
                    spacing: 6px;
                }
                QPushButton {
                    background-color: #282d37;
                    border: 1px solid #3a4150;
                    border-radius: 8px;
                    padding: 8px 14px;
                    font-weight: 600;
                    color: #e6e8ee;
                }
                QPushButton:hover {
                    background-color: #323845;
                }
                QPushButton#primaryBtn {
                    background-color: #2563eb;
                    border: 1px solid #3b82f6;
                    color: #ffffff;
                }
                QPushButton#primaryBtn:hover {
                    background-color: #1d4ed8;
                }
                QPushButton#miniBtn {
                    padding: 2px 8px;
                    font-size: 11px;
                    border-radius: 5px;
                }
                QPlainTextEdit {
                    background-color: #111317;
                    border: 1px solid #272c36;
                    border-radius: 8px;
                    color: #9ca3af;
                    font-family: monospace;
                    font-size: 11px;
                    padding: 6px;
                }
            """)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow(app)
    win.show()
    sys.exit(app.exec())


def run_tk_app():
    import tkinter as tk
    from tkinter import ttk, scrolledtext

    root = tk.Tk()
    root.title("USB Audio Stream")
    root.geometry("540x600")
    root.minsize(420, 440)
    root.configure(bg="#16181d")

    root.columnconfigure(0, weight=1)
    root.rowconfigure(5, weight=1)

    hdr = tk.Frame(root, bg="#16181d")
    hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
    tk.Label(hdr, text="USB Audio Stream", bg="#16181d", fg="#f3f4f6", font=("Sans", 14, "bold")).pack(anchor="w")
    tk.Label(hdr, text="Audio de PipeWire hacia Android por cable USB (ADB)", bg="#16181d", fg="#9aa0ae", font=("Sans", 9)).pack(anchor="w")

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

    log_box = scrolledtext.ScrolledText(root, height=6, bg="#111317", fg="#9ca3af", font=("Monospace", 8), borderwidth=1, relief="flat")
    log_box.grid(row=5, column=0, sticky="nsew", padx=18, pady=(0, 14))

    def append_log(msg):
        ts = time.strftime("%H:%M:%S")
        log_lines.append(f"[{ts}] {msg}")
        def _sync():
            log_box.delete("1.0", "end")
            log_box.insert("end", "\n".join(log_lines) + "\n")
            log_box.see("end")
        root.after(0, _sync)

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

    tk.Scale(
        gain_row, from_=-12, to=6, resolution=0.5, orient="horizontal",
        variable=gain_var, showvalue=True, bg="#1f232b", fg="#f3f4f6",
        highlightthickness=0, command=lambda v: ctrl.set_gain_db(float(v))
    ).pack(side="left", fill="x", expand=True, padx=8)

    tk.Checkbutton(
        gain_row, text="Limitador", variable=lim_var,
        bg="#1f232b", fg="#cfd5e2", selectcolor="#16181d",
        command=lambda: ctrl.set_soft_limiter(lim_var.get())
    ).pack(side="right")

    opts = list_audio_sources()
    for sid, lbl in opts:
        sources_map[lbl] = sid
    src_combo["values"] = [lbl for _, lbl in opts]
    if opts:
        src_combo.current(0)

    src_combo.bind("<<ComboboxSelected>>", lambda _e: ctrl.set_source_mode(sources_map.get(src_combo.get(), "__default__")))
    rate_combo.bind("<<ComboboxSelected>>", lambda _e: ctrl.set_rate_mode("native" if rate_combo.get() == "Nativa" else rate_combo.get()))
    tk.Button(lat_frame, text="Aplicar", bg="#282d37", fg="#e6e8ee", relief="flat",
              command=lambda: ctrl.set_latency(cap_spin.get(), chk_spin.get())).pack(side="right")

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

    root.bind("<Configure>", on_configure)

    missing = check_dependencies()
    if missing:
        update_status("error", f"Faltan herramientas: {', '.join(missing)}")
        append_log("Instala: sudo pacman -S --needed android-tools libpulse pipewire-pulse")
    else:
        ctrl.start()

    def on_close():
        ctrl.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main():
    for binding in ("PySide6", "PyQt6"):
        try:
            __import__(binding)
            run_qt_app(binding)
            return
        except ImportError:
            continue
    run_tk_app()


if __name__ == "__main__":
    main()
