import sys
import os
import re
import json
import time
import traceback
import math
import shutil
import socket
import subprocess
import threading
import pyaudiowpatch as pyaudio

import PySide6
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSlider, QCheckBox, QTextEdit,
    QProgressBar, QSystemTrayIcon, QMenu, QFrame, QScrollArea,
    QSpinBox, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QObject, QUrl, QTimer, QEvent
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QFont, QDesktopServices

from app_logger import (
    get_log_path,
    install_exception_hooks,
    log,
    log_startup_info,
    open_log_folder,
    setup_logging,
)
from app_icon import get_icon_path
from app_settings import load_settings, save_settings
from app_theme_qt import apply_theme
from app_version import APP_VERSION
from audio_capture import (
    CaptureFormatError,
    downmix_to_stereo,
    resolve_capture_format,
)
from dsp import process_pcm_s16le
from protocol import pack_header
from update_checker import RELEASES_URL, check_for_update
from update_window_qt import UpdateWindow
from theme import valid_theme
from updater import can_self_update

CREATE_NO_WINDOW = 0x08000000
os.environ.setdefault("ANDROID_ADB_SERVER_PORT", "5038")
DEFAULT_PORT = 5000
DEFAULT_RATE = 48000
AUDIO_RETRY_SECONDS = 3.0
MAX_LOG_LINES = 300
SEND_TIMEOUT_SECONDS = 5.0
SOCKET_BUFFER_BYTES = 32768


class StreamStalled(ConnectionError):
    """El móvil dejó de leer audio y el socket agotó el tiempo de espera."""


def boost_thread_priority():
    """Eleva la prioridad del hilo de audio en Windows (mejor esfuerzo)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        # THREAD_PRIORITY_HIGHEST = 2
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2))
    except Exception:
        return False


def get_adb_path() -> str:
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    local_adb = os.path.join(base_dir, "adb", "adb.exe")
    return local_adb if os.path.exists(local_adb) else "adb"


def _run_adb_cmd(exe: str, args: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [exe] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=CREATE_NO_WINDOW,
        timeout=timeout,
    )


def check_adb_health() -> tuple[str, str]:
    """
    Comprueba si ADB está instalado y funciona de verdad (binario ejecutable y
    servidor capaz de arrancar). Devuelve (estado, detalle), donde estado es
    'ok', 'missing' o 'error'.
    """
    path = get_adb_path()
    resolved = path if os.path.isabs(path) and os.path.exists(path) else shutil.which(path)
    missing_msg = (
        "ADB no está instalado. Descarga Android SDK Platform-Tools y añade su "
        "carpeta al PATH (o coloca adb.exe en una carpeta 'adb' junto al ejecutable)."
    )
    if not resolved:
        return "missing", missing_msg
    try:
        version_res = _run_adb_cmd(resolved, ["version"], 5)
        output = (version_res.stdout or "").strip()
        if version_res.returncode != 0:
            detail = output.splitlines()[-1] if output else f"código de salida {version_res.returncode}"
            return "error", f"ADB está instalado pero no funciona: {detail}"
        version = ""
        m = re.search(r"^Version\s+(\S+)", output, re.MULTILINE)
        if m:
            version = f" v{m.group(1)}"
        server_res = _run_adb_cmd(resolved, ["start-server"], 10)
        if server_res.returncode != 0:
            output = (server_res.stdout or "").strip()
            detail = output.splitlines()[-1] if output else f"código de salida {server_res.returncode}"
            return "error", f"ADB está instalado pero no funciona: {detail}"
        return "ok", f"ADB{version} instalado y funcionando ({resolved})."
    except subprocess.TimeoutExpired:
        return "error", "ADB está instalado pero no responde (tiempo de espera agotado)."
    except FileNotFoundError:
        return "missing", missing_msg
    except Exception as e:
        return "error", f"ADB está instalado pero no funciona: {e}"


class WorkerSignals(QObject):
    log = Signal(str)
    status_changed = Signal(str, str, str)
    level_updated = Signal(float, bool)
    adb_checked = Signal(str, str)
    update_checked = Signal(str)


class AudioStreamWorker(threading.Thread):
    def __init__(self, signals: WorkerSignals):
        super().__init__(daemon=True)
        self.signals = signals
        self.running = True
        self.paused = False
        self.force_reconnect = False
        self.device_index = None
        self.chunk_ms = 15
        self.tcp_port = DEFAULT_PORT
        self.gain_db = 0.0
        self.gain_linear = 1.0
        self.muted = False
        self.soft_limiter = True
        self._lock = threading.Lock()
        self._sock = None
        self._stream = None
        self._last_audio_error = None
        self._pa = pyaudio.PyAudio()

    def set_device(self, device_index: int):
        with self._lock:
            if self.device_index != device_index:
                self.device_index = device_index
                self.force_reconnect = True

    def set_chunk_ms(self, ms: int):
        with self._lock:
            if self.chunk_ms != ms:
                self.chunk_ms = ms
                self.force_reconnect = True

    def set_gain_db(self, db: float):
        self.gain_db = float(db)
        self._update_gain_linear()

    def set_muted(self, muted: bool):
        self.muted = bool(muted)
        self._update_gain_linear()

    def _update_gain_linear(self):
        self.gain_linear = (
            0.0 if self.muted else math.pow(10.0, self.gain_db / 20.0)
        )

    def set_soft_limiter(self, enabled: bool):
        self.soft_limiter = enabled

    def set_paused(self, paused: bool):
        self.paused = paused
        if paused:
            self.force_reconnect = True

    def trigger_reconnect(self):
        self.paused = False
        self.force_reconnect = True

    def stop(self):
        self.running = False
        self._close_connections()
        try:
            self._pa.terminate()
        except Exception:
            pass

    def _close_connections(self):
        if self._stream:
            try:
                if self._stream.is_active():
                    self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _run_adb(self, args: list) -> tuple[int, str]:
        try:
            res = subprocess.run(
                [get_adb_path()] + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=4
            )
            return res.returncode, res.stdout.strip()
        except Exception as e:
            return -1, str(e)

    def _check_android_device(self) -> tuple[str, str | None]:
        code, out = self._run_adb(["devices"])
        if code != 0:
            return "error", out

        devices = []
        for line in out.splitlines()[1:]:
            parts = [part for part in line.strip().split("\t") if part]
            if len(parts) >= 2:
                devices.append((parts[0], parts[1]))

        if not devices:
            return "none", None

        chosen = next((item for item in devices if item[1] == "device"), None)
        if chosen is None:
            chosen = next((item for item in devices if item[1] == "unauthorized"), None)
        if chosen is None:
            chosen = devices[0]

        serial, state = chosen
        if state == "device":
            _, model = self._run_adb(["-s", serial, "shell", "getprop", "ro.product.model"])
            return "device", f"{model} ({serial})" if model else serial
        return state, serial

    def _setup_adb_forward(self, serial_info: str) -> bool:
        serial = serial_info.split("(")[-1].rstrip(")") if "(" in serial_info else serial_info
        code, out = self._run_adb(["-s", serial, "forward", f"tcp:{self.tcp_port}", f"tcp:{self.tcp_port}"])
        if code != 0:
            self.signals.log.emit(f"[ADB] Error en port-forward: {out}")
            return False
        return True

    def _process_audio(self, raw_data: bytes) -> tuple[bytes, float, bool]:
        out_bytes, level, clipping = process_pcm_s16le(
            raw_data, self.gain_linear, self.soft_limiter, True
        )
        return out_bytes, level * 100.0, clipping

    def _log_audio_open_error(self, dev_idx, dev_info, chunk_ms, error, fmt=None):
        """Registra el contexto completo de un fallo al abrir el dispositivo."""
        hostapi_idx = dev_info.get("hostApi", "?")
        try:
            hostapi_name = self._pa.get_host_api_info_by_index(hostapi_idx)["name"]
        except Exception:
            hostapi_name = "desconocida"
        is_loopback = bool(dev_info.get("isLoopbackDevice", False))

        self.signals.log.emit(
            f"[Audio] Fallo al abrir el dispositivo (índice {dev_idx}): "
            f"{dev_info.get('name', 'sin nombre')}"
        )
        self.signals.log.emit(
            f"[Audio]   hostapi={hostapi_name} (idx {hostapi_idx}) | "
            f"maxInputChannels={dev_info.get('maxInputChannels')} | "
            f"maxOutputChannels={dev_info.get('maxOutputChannels')} | "
            f"defaultSampleRate={dev_info.get('defaultSampleRate')} | "
            f"isLoopback={is_loopback}"
        )
        if fmt is not None:
            self.signals.log.emit(
                f"[Audio]   stream solicitado: format=paInt16 | "
                f"channels={fmt.capture_channels} | rate={fmt.rate} | "
                f"frames_per_buffer={fmt.frames_per_buffer} | chunk_ms={chunk_ms}"
            )
        else:
            self.signals.log.emit(
                "[Audio]   no se abrió el stream: formato de captura inválido"
            )
        self.signals.log.emit(f"[Audio]   error: {error}")
        self.signals.log.emit(
            "[Audio]   no se probará otra fuente automáticamente; "
            f"reintento en {AUDIO_RETRY_SECONDS:.1f} s"
        )

    def run(self):
        if boost_thread_priority():
            self.signals.log.emit("[Audio] Hilo de audio con prioridad alta")
        last_state = ""
        while self.running:
            if self.paused:
                self._close_connections()
                if last_state != "paused":
                    self.signals.status_changed.emit("Detenido", "Transmision en pausa", "#95A5A6")
                    last_state = "paused"
                self.signals.level_updated.emit(0.0, False)
                time.sleep(0.4)
                continue

            self.force_reconnect = False
            state, info = self._check_android_device()
            if state != "device" or not info:
                msg = "Autoriza depuracion USB en el movil" if state == "unauthorized" else "Ningun dispositivo ADB"
                color = "#F39C12" if state == "unauthorized" else "#E74C3C"
                if last_state != f"dev_{state}":
                    self.signals.status_changed.emit("Esperando USB", msg, color)
                    self.signals.log.emit(f"[ADB] {msg}...")
                    last_state = f"dev_{state}"
                self.signals.level_updated.emit(0.0, False)
                time.sleep(1.5)
                continue

            if not self._setup_adb_forward(info):
                time.sleep(1.5)
                continue

            with self._lock:
                dev_idx = self.device_index
                chunk_ms = self.chunk_ms

            if dev_idx is None:
                time.sleep(0.5)
                continue

            try:
                dev_info = self._pa.get_device_info_by_index(dev_idx)
                try:
                    fmt = resolve_capture_format(dev_info, chunk_ms, DEFAULT_RATE)
                except CaptureFormatError as fmt_err:
                    error_key = ("formato", dev_idx, str(fmt_err))
                    if self._last_audio_error != error_key:
                        self._last_audio_error = error_key
                        self._log_audio_open_error(dev_idx, dev_info, chunk_ms, fmt_err)
                        self.signals.status_changed.emit(
                            "Error de audio",
                            f"{dev_info.get('name', 'Fuente no válida')} (ver registro)",
                            "#E74C3C",
                        )
                    last_state = f"audio_error_{dev_idx}_{chunk_ms}"
                    self.signals.level_updated.emit(0.0, False)
                    time.sleep(AUDIO_RETRY_SECONDS)
                    continue

                sock = socket.create_connection(("127.0.0.1", self.tcp_port), timeout=3.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_BYTES
                )
                self._sock = sock

                ack = sock.recv(1)
                if ack != b"R":
                    raise ConnectionError("La app Android no respondio R")

                sock.settimeout(SEND_TIMEOUT_SECONDS)
                sock.sendall(pack_header(fmt.rate, fmt.send_channels, chunk_ms))

                try:
                    self._stream = self._pa.open(
                        format=pyaudio.paInt16,
                        channels=fmt.capture_channels,
                        rate=fmt.rate,
                        input=True,
                        input_device_index=dev_idx,
                        frames_per_buffer=fmt.frames_per_buffer,
                    )
                except OSError as audio_err:
                    # Sin fallback: si falla la fuente elegida (p. ej. un
                    # [Loopback]) no se transmite el micrófono por sorpresa.
                    error_key = (
                        "apertura",
                        dev_idx,
                        fmt.rate,
                        fmt.capture_channels,
                        fmt.send_channels,
                        chunk_ms,
                        str(audio_err),
                    )
                    if self._last_audio_error != error_key:
                        self._last_audio_error = error_key
                        self._log_audio_open_error(
                            dev_idx, dev_info, chunk_ms, audio_err, fmt
                        )
                        self.signals.status_changed.emit(
                            "Error de audio",
                            f"{dev_info.get('name', 'Dispositivo')} (ver registro)",
                            "#E74C3C",
                        )
                    last_state = f"audio_error_{dev_idx}_{chunk_ms}"
                    self._close_connections()
                    self.signals.level_updated.emit(0.0, False)
                    time.sleep(AUDIO_RETRY_SECONDS)
                    continue

                self._last_audio_error = None

                dev_title = dev_info["name"]
                self.signals.status_changed.emit("Transmitiendo", f"{info} ({fmt.rate} Hz)", "#2ECC71")
                if last_state != f"streaming_{info}_{dev_idx}_{chunk_ms}":
                    channel_note = (
                        f"{fmt.capture_channels}ch→{fmt.send_channels}ch"
                        if fmt.capture_channels != fmt.send_channels
                        else f"{fmt.send_channels}ch"
                    )
                    self.signals.log.emit(
                        f"[Audio] Transmitiendo {dev_title} "
                        f"({fmt.rate}Hz, {channel_note}, {chunk_ms}ms) -> tcp:{self.tcp_port}"
                    )
                    last_state = f"streaming_{info}_{dev_idx}_{chunk_ms}"

                last_ui = 0.0
                while self.running and not self.force_reconnect and not self.paused:
                    raw_data = self._stream.read(
                        fmt.frames_per_buffer, exception_on_overflow=False
                    )
                    if fmt.capture_channels != fmt.send_channels:
                        raw_data = downmix_to_stereo(
                            raw_data, fmt.capture_channels
                        )
                    out_bytes, lvl, clip = self._process_audio(raw_data)
                    try:
                        self._sock.sendall(out_bytes)
                    except socket.timeout as exc:
                        raise StreamStalled(
                            "El móvil dejó de leer audio durante "
                            f"{SEND_TIMEOUT_SECONDS:.0f} s; reconectando"
                        ) from exc
                    now = time.monotonic()
                    if (now - last_ui) >= 0.06:
                        self.signals.level_updated.emit(lvl, clip)
                        last_ui = now

            except StreamStalled as e:
                self.signals.status_changed.emit(
                    "Reconectando...", "El móvil no responde", "#F39C12"
                )
                self.signals.log.emit(f"[Stream] {e}")
                self._close_connections()
                self.signals.level_updated.emit(0.0, False)
                time.sleep(1.0)
            except (ConnectionRefusedError, ConnectionError, socket.timeout, BrokenPipeError) as e:
                if last_state != "waiting_app":
                    self.signals.status_changed.emit("Reconectando...", info, "#F39C12")
                    self.signals.log.emit(f"[Stream] Esperando app Android en puerto {self.tcp_port} ({e})")
                    last_state = "waiting_app"
                self._close_connections()
                self.signals.level_updated.emit(0.0, False)
                time.sleep(1.2)
            except OSError as e:
                self.signals.status_changed.emit("Error de audio/red", str(e), "#E74C3C")
                self.signals.log.emit(f"[Audio/OS] {e}")
                self.signals.log.emit(traceback.format_exc())
                self._close_connections()
                self.signals.level_updated.emit(0.0, False)
                time.sleep(1.5)
            except Exception as e:
                self.signals.log.emit(f"[Error] {e}")
                self.signals.log.emit(traceback.format_exc())
                self._close_connections()
                time.sleep(1.5)
            finally:
                self._close_connections()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SpeakerPhone-USB v{APP_VERSION} (Windows)")
        self.setMinimumSize(540, 520)
        self._update_url = RELEASES_URL
        self._latest_update = None
        self._update_window = None
        self._auto_prompted_version = ""
        self._theme = valid_theme(load_settings().get("theme", "dark"))
        self._settings_ready = False
        self._pending_level = 0.0
        self._pending_clip = False
        self._badge_state = None
        self.signals = WorkerSignals()
        self.worker = AudioStreamWorker(self.signals)
        self._init_ui()
        self._apply_accessibility()
        self._apply_theme()
        self._init_tray()
        self._connect_signals()
        self._populate_audio_devices()
        self._refresh_adb_status()
        self._check_updates()
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(6 * 60 * 60 * 1000)
        self.update_timer.timeout.connect(self._check_updates)
        self.update_timer.start()
        self._apply_saved_settings()
        self._settings_ready = True
        self.worker.start()

    def _create_app_icon(self, color_hex="#2ECC71") -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        icon_path = get_icon_path()
        base = QPixmap(icon_path) if icon_path else QPixmap()
        if not base.isNull():
            painter.drawPixmap(
                0,
                0,
                base.scaled(
                    64,
                    64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ),
            )
        else:
            painter.setBrush(QColor(color_hex))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(6, 6, 52, 52, 14, 14)
            painter.setPen(QColor("#FFFFFF"))
            painter.setFont(QFont("Segoe UI", 24, QFont.Bold))
            painter.drawText(pixmap.rect(), Qt.AlignCenter, "USB")

        painter.setPen(QColor("#0f172a"))
        painter.setBrush(QColor(color_hex))
        painter.drawEllipse(42, 42, 18, 18)
        painter.end()
        return QIcon(pixmap)

    def _init_ui(self):
        self.setWindowIcon(self._create_app_icon())
        self.setMinimumSize(540, 640)
        self.resize(580, 740)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # Cabecera
        title = QLabel("SpeakerPhone-USB")
        title.setObjectName("titleLabel")
        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setObjectName("versionLabel")
        self.version_label.setAccessibleName(f"Versión {APP_VERSION}")
        self.theme_button = QPushButton("☀ Claro")
        self.theme_button.setObjectName("miniBtn")
        self.theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_button.setToolTip("Cambiar entre tema oscuro y claro")
        self.theme_button.setAccessibleName("Cambiar tema")
        self.theme_button.clicked.connect(self._toggle_theme)
        title_row = QHBoxLayout()
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.theme_button)
        title_row.addWidget(self.version_label, 0, Qt.AlignmentFlag.AlignVCenter)

        subtitle = QLabel(
            "Audio del sistema (WASAPI) hacia Android por cable USB (ADB) • "
            "Reconexión automática"
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)

        update_row = QHBoxLayout()
        update_row.setSpacing(8)
        self.update_label = QLabel("Buscando actualizaciones...")
        self.update_label.setObjectName("hintLabel")
        self.update_label.setWordWrap(True)
        self.update_check_button = QPushButton("Buscar actualizaciones")
        self.update_check_button.setObjectName("miniBtn")
        self.update_check_button.setMinimumWidth(150)
        self.update_check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_check_button.setToolTip(
            "Volver a comprobar si hay una versión nueva"
        )
        self.update_check_button.clicked.connect(self._check_updates)
        self.update_button = QPushButton("Descargar")
        self.update_button.setObjectName("miniBtn")
        self.update_button.setMinimumWidth(100)
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setVisible(False)
        self.update_button.clicked.connect(self._open_update_window)
        update_row.addWidget(self.update_label, 1)
        update_row.addWidget(self.update_check_button)
        update_row.addWidget(self.update_button)

        layout.addLayout(title_row)
        layout.addWidget(subtitle)
        layout.addLayout(update_row)

        # Tarjeta: conexión, ADB y nivel
        conn_card = QFrame()
        conn_card.setObjectName("card")
        conn_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        cc = QVBoxLayout(conn_card)
        cc.setContentsMargins(14, 12, 14, 12)
        cc.setSpacing(8)

        dev_title = QLabel("DISPOSITIVO CONECTADO")
        dev_title.setObjectName("sectionLabel")
        self.device_label = QLabel("Buscando dispositivo...")
        self.device_label.setObjectName("valueLabel")
        self.device_label.setWordWrap(True)
        cc.addWidget(dev_title)
        cc.addWidget(self.device_label)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setObjectName("sep")
        cc.addWidget(sep1)

        st_title = QLabel("ESTADO DE CONEXIÓN")
        st_title.setObjectName("sectionLabel")
        st_row = QHBoxLayout()
        st_row.setSpacing(8)
        self.status_dot = QLabel("●")
        self.status_dot.setFixedWidth(16)
        self.status_dot.setStyleSheet("color: #E74C3C; font-size: 15px;")
        self.status_label = QLabel("Iniciando...")
        self.status_label.setObjectName("valueLabel")
        self.status_label.setWordWrap(True)
        st_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        st_row.addWidget(self.status_label, 1)
        cc.addWidget(st_title)
        cc.addLayout(st_row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setObjectName("sep")
        cc.addWidget(sep2)

        adb_hdr = QHBoxLayout()
        adb_hdr.setSpacing(8)
        adb_title = QLabel("ADB (DEPURACIÓN USB)")
        adb_title.setObjectName("sectionLabel")
        self.adb_badge = QLabel("ADB: comprobando...")
        self.adb_badge.setObjectName("adbNeutral")
        self.btn_adb_check = QPushButton("↻")
        self.btn_adb_check.setObjectName("miniBtn")
        self.btn_adb_check.setFixedWidth(30)
        self.btn_adb_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_adb_check.setToolTip(
            "Volver a comprobar si adb está instalado y funciona"
        )
        self.btn_adb_check.setAccessibleName("Volver a comprobar ADB")
        adb_hdr.addWidget(adb_title)
        adb_hdr.addStretch(1)
        adb_hdr.addWidget(self.adb_badge)
        adb_hdr.addWidget(self.btn_adb_check)
        cc.addLayout(adb_hdr)

        self.adb_detail = QLabel("Verificando si adb está instalado y responde...")
        self.adb_detail.setObjectName("hintLabel")
        self.adb_detail.setWordWrap(True)
        cc.addWidget(self.adb_detail)

        lvl_header = QHBoxLayout()
        lvl_title = QLabel("NIVEL DE SEÑAL Y SATURACIÓN")
        lvl_title.setObjectName("sectionLabel")
        self.clip_badge = QLabel("SEÑAL OK")
        self.clip_badge.setObjectName("clipOk")
        self.clip_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lvl_header.addWidget(lvl_title)
        lvl_header.addStretch(1)
        lvl_header.addWidget(self.clip_badge)
        cc.addLayout(lvl_header)

        self.vu_meter = QProgressBar()
        self.vu_meter.setRange(0, 100)
        self.vu_meter.setTextVisible(False)
        self.vu_meter.setFixedHeight(10)
        self.vu_meter.setObjectName("vuNormal")
        cc.addWidget(self.vu_meter)

        layout.addWidget(conn_card)

        # Tarjeta: fuente, ganancia y latencia
        audio_card = QFrame()
        audio_card.setObjectName("card")
        audio_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        ac = QVBoxLayout(audio_card)
        ac.setContentsMargins(14, 12, 14, 12)
        ac.setSpacing(10)

        src_title = QLabel("FUENTE DE AUDIO Y FRECUENCIA")
        src_title.setObjectName("sectionLabel")
        ac.addWidget(src_title)

        src_row = QHBoxLayout()
        src_row.setSpacing(8)
        self.combo_devices = QComboBox()
        self.combo_devices.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.combo_devices.setToolTip(
            "Salidas del sistema (loopback) y micrófonos WASAPI"
        )
        self.btn_refresh_dev = QPushButton("↻")
        self.btn_refresh_dev.setObjectName("miniBtn")
        self.btn_refresh_dev.setFixedWidth(36)
        self.btn_refresh_dev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh_dev.setToolTip("Actualizar fuentes de audio")
        self.btn_refresh_dev.setAccessibleName("Actualizar fuentes de audio")
        src_row.addWidget(self.combo_devices, 1)
        src_row.addWidget(self.btn_refresh_dev)
        ac.addLayout(src_row)

        self.source_hint = QLabel(
            "Loopback: usa el formato nativo del endpoint y no cambia al micrófono."
        )
        self.source_hint.setObjectName("hintLabel")
        self.source_hint.setWordWrap(True)
        ac.addWidget(self.source_hint)

        gain_row = QHBoxLayout()
        gain_row.setSpacing(8)
        gain_row.addWidget(QLabel("Ganancia:"))
        self.slider_gain = QSlider(Qt.Orientation.Horizontal)
        self.slider_gain.setRange(-120, 120)
        self.slider_gain.setValue(0)
        self.slider_gain.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.label_gain = QLabel("0.0 dB")
        self.label_gain.setMinimumWidth(52)
        self.label_gain.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        gain_row.addWidget(self.slider_gain, 1)
        gain_row.addWidget(self.label_gain)
        ac.addLayout(gain_row)

        gain_tools = QHBoxLayout()
        gain_tools.setSpacing(8)
        self.mute_btn = QPushButton("Silenciar")
        self.mute_btn.setObjectName("miniBtn")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute_btn.setToolTip(
            "Silenciar la salida sin perder el nivel de ganancia"
        )
        self.mute_btn.setAccessibleName("Silenciar salida de audio")
        self.reset_gain_btn = QPushButton("0.0 dB")
        self.reset_gain_btn.setObjectName("miniBtn")
        self.reset_gain_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_gain_btn.setToolTip("Restablecer la ganancia a 0.0 dB")
        self.reset_gain_btn.setAccessibleName("Restablecer ganancia a 0.0 dB")
        self.chk_limiter = QCheckBox("Limitador suave")
        self.chk_limiter.setChecked(True)
        self.chk_limiter.setToolTip(
            "Suaviza picos altos antes de enviarlos para evitar distorsión"
        )
        gain_tools.addWidget(self.mute_btn)
        gain_tools.addWidget(self.reset_gain_btn)
        gain_tools.addWidget(self.chk_limiter)
        gain_tools.addStretch(1)
        ac.addLayout(gain_tools)

        row_latency = QHBoxLayout()
        row_latency.addWidget(QLabel("Perfil de búfer:"))
        self.combo_profile = QComboBox()
        self.combo_profile.addItem("Baja latencia (8 ms)", ("low", 8))
        self.combo_profile.addItem("Equilibrado (15 ms)", ("balanced", 15))
        self.combo_profile.addItem("Alta estabilidad (30 ms)", ("stable", 30))
        self.combo_profile.addItem(
            "Máxima estabilidad (60 ms)", ("max_stable", 60)
        )
        self.combo_profile.setItemData(0, "Mínima latencia (juegos)", Qt.ItemDataRole.ToolTipRole)
        self.combo_profile.setItemData(1, "Balance latencia/estabilidad", Qt.ItemDataRole.ToolTipRole)
        self.combo_profile.setItemData(2, "Mayor margen contra cortes USB", Qt.ItemDataRole.ToolTipRole)
        self.combo_profile.setItemData(
            3, "Resiste picos de CPU y bloqueos breves", Qt.ItemDataRole.ToolTipRole
        )
        self.combo_profile.setCurrentIndex(1)
        row_latency.addWidget(self.combo_profile, 1)
        ac.addLayout(row_latency)

        custom_row = QHBoxLayout()
        custom_row.setSpacing(8)
        custom_row.addWidget(QLabel("Búfer / paquete:"))
        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(2, 100)
        self.chunk_spin.setValue(15)
        self.chunk_spin.setSuffix(" ms")
        self.chunk_spin.setToolTip(
            "Búfer de captura y paquete enviado; más ms resiste mejor los picos de CPU"
        )
        self.chunk_spin.setAccessibleName("Búfer en milisegundos")
        self.apply_buffer_btn = QPushButton("Aplicar")
        self.apply_buffer_btn.setObjectName("miniBtn")
        self.apply_buffer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_buffer_btn.setToolTip("Aplicar el búfer personalizado")
        self.apply_buffer_btn.setAccessibleName("Aplicar búfer personalizado")
        custom_row.addWidget(self.chunk_spin)
        custom_row.addWidget(self.apply_buffer_btn)
        custom_row.addStretch(1)
        ac.addLayout(custom_row)

        layout.addWidget(audio_card)

        # Botones principales
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.btn_retry = QPushButton("Reintentar conexión")
        self.btn_retry.setObjectName("primaryBtn")
        self.btn_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_retry.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.btn_toggle = QPushButton("Detener")
        self.btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        btn_row.addWidget(self.btn_retry, 2)
        btn_row.addWidget(self.btn_toggle, 1)
        layout.addLayout(btn_row)

        # Registro
        log_hdr = QHBoxLayout()
        log_lbl = QLabel("LOG EN VIVO")
        log_lbl.setObjectName("sectionLabel")
        btn_open_logs = QPushButton("Abrir carpeta")
        btn_open_logs.setObjectName("miniBtn")
        btn_open_logs.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_logs.setToolTip(get_log_path() or "Carpeta de logs no disponible")
        btn_open_logs.setAccessibleName("Abrir carpeta de logs")
        btn_open_logs.clicked.connect(self._open_log_folder)
        self.copy_log_btn = QPushButton("Copiar")
        self.copy_log_btn.setObjectName("miniBtn")
        self.copy_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_log_btn.setToolTip("Copiar el registro visible al portapapeles")
        self.copy_log_btn.setAccessibleName("Copiar registro")
        self.clear_log_btn = QPushButton("Limpiar")
        self.clear_log_btn.setObjectName("miniBtn")
        self.clear_log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_log_btn.setToolTip("Vaciar el registro en vivo")
        self.clear_log_btn.setAccessibleName("Limpiar registro")
        log_hdr.addWidget(log_lbl)
        log_hdr.addStretch(1)
        log_hdr.addWidget(btn_open_logs)
        log_hdr.addWidget(self.copy_log_btn)
        log_hdr.addWidget(self.clear_log_btn)
        layout.addLayout(log_hdr)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setMinimumHeight(90)
        self.log_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.log_view.setPlaceholderText(
            "Eventos de conexión y audio en tiempo real..."
        )
        layout.addWidget(self.log_view, 1)

    def _init_tray(self):
        self.tray_icon = QSystemTrayIcon(self._create_app_icon("#E74C3C"), self)
        self.tray_icon.setToolTip(f"SpeakerPhone-USB v{APP_VERSION}")
        tray_menu = QMenu()
        a_show = QAction("Mostrar / Ocultar", self)
        a_show.triggered.connect(self._toggle_visibility)
        a_toggle = QAction("Iniciar / Detener", self)
        a_toggle.triggered.connect(self._on_toggle)
        a_rec = QAction("Reconectar USB", self)
        a_rec.triggered.connect(self.worker.trigger_reconnect)
        self.tray_mute_action = QAction("Silenciar audio", self)
        self.tray_mute_action.triggered.connect(self.mute_btn.toggle)
        a_quit = QAction("Salir", self)
        a_quit.triggered.connect(self._quit_app)
        tray_menu.addAction(a_show)
        tray_menu.addSeparator()
        tray_menu.addAction(a_toggle)
        tray_menu.addAction(a_rec)
        tray_menu.addAction(self.tray_mute_action)
        tray_menu.addSeparator()
        tray_menu.addAction(a_quit)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(lambda r: self._toggle_visibility() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray_icon.show()

    def _connect_signals(self):
        self.signals.log.connect(self._append_log)
        self.signals.status_changed.connect(self._update_status)
        self.signals.level_updated.connect(self._update_meter)
        self.signals.adb_checked.connect(self._on_adb_checked)
        self.signals.update_checked.connect(self._on_update_checked)
        self.btn_retry.clicked.connect(self._on_retry)
        self.btn_toggle.clicked.connect(self._on_toggle)
        self.btn_adb_check.clicked.connect(self._refresh_adb_status)
        self.btn_refresh_dev.clicked.connect(self._populate_audio_devices)
        self.combo_devices.currentIndexChanged.connect(self._on_device_changed)
        self.slider_gain.valueChanged.connect(self._on_gain_changed)
        self.chk_limiter.toggled.connect(self._on_limiter_toggled)
        self.combo_profile.currentIndexChanged.connect(self._on_profile_changed)
        self.mute_btn.toggled.connect(self._on_mute_toggled)
        self.reset_gain_btn.clicked.connect(self._on_reset_gain)
        self.apply_buffer_btn.clicked.connect(self._on_apply_buffer)
        self.copy_log_btn.clicked.connect(self._copy_log)
        self.clear_log_btn.clicked.connect(self._clear_log)

    def _populate_audio_devices(self):
        self.combo_devices.blockSignals(True)
        self.combo_devices.clear()
        pa = self.worker._pa
        try:
            wasapi_idx = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        except OSError:
            wasapi_idx = None
        default_idx = 0
        count = 0
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if wasapi_idx is not None and dev["hostApi"] != wasapi_idx:
                continue
            if dev["maxInputChannels"] > 0:
                is_lb = dev.get("isLoopbackDevice", False)
                prefix = "[Sistema / Loopback]" if is_lb else "[Microfono]"
                dev_name = dev["name"]
                self.combo_devices.addItem(f"{prefix} {dev_name}", userData=i)
                if is_lb and default_idx == 0:
                    default_idx = count
                count += 1
        self.combo_devices.blockSignals(False)
        if self.combo_devices.count() > 0:
            saved_name = load_settings().get("device_name")
            saved_index = self.combo_devices.findText(saved_name) if saved_name else -1
            index = saved_index if saved_index >= 0 else default_idx
            self.combo_devices.setCurrentIndex(index)
            self._on_device_changed(index)

    def _apply_saved_settings(self):
        settings = load_settings()

        gain_db = settings.get("gain_db")
        if isinstance(gain_db, (int, float)):
            value = int(round(max(-12.0, min(12.0, float(gain_db))) * 10))
            self.slider_gain.setValue(value)

        muted = settings.get("muted")
        if isinstance(muted, bool):
            self.mute_btn.setChecked(muted)

        limiter = settings.get("soft_limiter")
        if isinstance(limiter, bool):
            self.chk_limiter.setChecked(limiter)

        profile = settings.get("profile")
        if profile:
            for index in range(self.combo_profile.count()):
                data = self.combo_profile.itemData(index)
                if data and data[0] == profile:
                    self.combo_profile.setCurrentIndex(index)
                    break

        chunk_ms = settings.get("chunk_ms")
        if isinstance(chunk_ms, int):
            chunk_ms = max(2, min(100, chunk_ms))
            self.chunk_spin.setValue(chunk_ms)
            self.worker.set_chunk_ms(chunk_ms)

        device_name = settings.get("device_name")
        if device_name:
            index = self.combo_devices.findText(device_name)
            if index >= 0:
                self.combo_devices.setCurrentIndex(index)

    def _save_settings(self):
        if not self._settings_ready:
            return
        profile_data = self.combo_profile.currentData()
        save_settings(
            {
                "gain_db": self.slider_gain.value() / 10.0,
                "muted": self.mute_btn.isChecked(),
                "soft_limiter": self.chk_limiter.isChecked(),
                "profile": profile_data[0] if profile_data else "",
                "chunk_ms": self.chunk_spin.value(),
                "device_name": self.combo_devices.currentText(),
                "theme": self._theme,
            }
        )

    def _apply_theme(self):
        apply_theme(QApplication.instance(), self._theme)
        if self._theme == "light":
            self.theme_button.setText("🌙 Oscuro")
            self.theme_button.setToolTip("Cambiar al tema oscuro")
            self.theme_button.setAccessibleDescription("Cambiar al tema oscuro")
        else:
            self.theme_button.setText("☀ Claro")
            self.theme_button.setToolTip("Cambiar al tema claro")
            self.theme_button.setAccessibleDescription("Cambiar al tema claro")

    def _toggle_theme(self):
        self._theme = "light" if self._theme == "dark" else "dark"
        self._apply_theme()
        self._save_settings()

    def _apply_accessibility(self):
        self.setAccessibleName("SpeakerPhone-USB")
        self.device_label.setAccessibleName("Dispositivo conectado")
        self.status_label.setAccessibleName("Estado de conexión")
        self.adb_badge.setAccessibleName("Estado de ADB")
        self.adb_detail.setAccessibleName("Detalle del estado de ADB")
        self.update_label.setAccessibleName("Estado de actualizaciones")
        self.update_button.setAccessibleName("Actualizar aplicación")
        self.update_check_button.setAccessibleName("Buscar actualizaciones")
        self.combo_devices.setAccessibleName("Fuente de audio")
        self.combo_profile.setAccessibleName("Perfil de búfer")
        self.slider_gain.setAccessibleName("Ganancia")
        self.chk_limiter.setAccessibleName("Limitador suave")
        self.mute_btn.setAccessibleName("Silenciar salida de audio")
        self.reset_gain_btn.setAccessibleName("Restablecer ganancia a 0.0 dB")
        self.chunk_spin.setAccessibleName("Búfer en milisegundos")
        self.apply_buffer_btn.setAccessibleName("Aplicar búfer personalizado")
        self.btn_refresh_dev.setAccessibleName("Actualizar fuentes de audio")
        self.vu_meter.setAccessibleName("Nivel de señal")
        self.clip_badge.setAccessibleName("Estado de saturación")
        self.btn_retry.setAccessibleName("Reintentar conexión")
        self.btn_toggle.setAccessibleName("Iniciar o detener transmisión")
        self.copy_log_btn.setAccessibleName("Copiar registro")
        self.clear_log_btn.setAccessibleName("Limpiar registro")
        self.log_view.setAccessibleName("Registro de eventos")

    def _on_device_changed(self, combo_idx: int):
        dev_idx = self.combo_devices.itemData(combo_idx)
        if dev_idx is not None:
            self.worker.set_device(dev_idx)
            self._save_settings()

    def _on_gain_changed(self, val: int):
        db = val / 10.0
        self.label_gain.setText(f"{db:+.1f} dB")
        self.worker.set_gain_db(db)
        self._save_settings()

    def _on_limiter_toggled(self, enabled: bool):
        self.worker.set_soft_limiter(enabled)
        self._save_settings()

    def _on_mute_toggled(self, muted: bool):
        self.worker.set_muted(muted)
        self._sync_mute_text(muted)
        self._save_settings()

    def _sync_mute_text(self, muted: bool):
        if muted:
            self.mute_btn.setText("Reactivar")
            self.mute_btn.setToolTip(
                "Quitar el silencio y volver al nivel de ganancia anterior"
            )
            self.mute_btn.setAccessibleName("Reactivar salida de audio")
        else:
            self.mute_btn.setText("Silenciar")
            self.mute_btn.setToolTip(
                "Silenciar la salida sin perder el nivel de ganancia"
            )
            self.mute_btn.setAccessibleName("Silenciar salida de audio")
        action = getattr(self, "tray_mute_action", None)
        if action is not None:
            action.setText("Reactivar audio" if muted else "Silenciar audio")

    def _on_reset_gain(self):
        self.slider_gain.setValue(0)

    def _on_apply_buffer(self):
        self.worker.set_chunk_ms(self.chunk_spin.value())
        self._save_settings()

    def _on_profile_changed(self, idx: int):
        data = self.combo_profile.itemData(idx)
        if data:
            self.chunk_spin.setValue(int(data[1]))
            self.worker.set_chunk_ms(int(data[1]))
            self._save_settings()

    def _on_retry(self):
        self.btn_toggle.setText("Detener")
        self.worker.trigger_reconnect()

    def _on_toggle(self):
        if self.worker.paused:
            self.btn_toggle.setText("Detener")
            self.worker.set_paused(False)
        else:
            self.btn_toggle.setText("Iniciar")
            self.worker.set_paused(True)

    def _append_log(self, msg: str):
        self.log_view.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        log(msg)

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self._append_log("Registro copiado al portapapeles.")

    def _clear_log(self):
        self.log_view.clear()

    def _open_log_folder(self):
        if not open_log_folder():
            self._append_log("No se pudo abrir la carpeta de logs.")

    def _refresh_adb_status(self):
        """Lanza la comprobación de ADB en segundo plano para no bloquear la UI."""
        self.adb_badge.setText("ADB: comprobando...")
        self.adb_badge.setObjectName("adbNeutral")
        self._repolish(self.adb_badge)
        self.adb_detail.setText("Verificando si adb está instalado y responde...")
        threading.Thread(target=self._adb_check_worker, daemon=True).start()

    def _adb_check_worker(self):
        state, detail = check_adb_health()
        self.signals.adb_checked.emit(state, detail)

    def _on_adb_checked(self, state: str, detail: str):
        styles = {
            "ok": ("ADB OK", "adbOk"),
            "missing": ("ADB NO INSTALADO", "adbError"),
            "error": ("ADB CON ERROR", "adbWarn"),
        }
        text, object_name = styles.get(state, ("ADB: ?", "adbNeutral"))
        self.adb_badge.setText(text)
        self.adb_badge.setObjectName(object_name)
        self._repolish(self.adb_badge)
        self.adb_detail.setText(detail)
        self._append_log(f"[ADB] {detail}")

    def _check_updates(self):
        """Consulta GitHub Releases en segundo plano."""
        self.update_label.setText("Buscando actualizaciones...")
        self.update_button.setVisible(False)
        self.update_check_button.setEnabled(False)
        self.update_check_button.setText("Buscando...")
        threading.Thread(target=self._update_check_worker, daemon=True).start()

    def _update_check_worker(self):
        result = check_for_update(APP_VERSION)
        self.signals.update_checked.emit(json.dumps(result))

    def _on_update_checked(self, payload: str):
        try:
            result = json.loads(payload)
        except Exception:
            result = {"status": "error", "error": "respuesta inválida"}

        status = result.get("status")
        latest = result.get("latest") or ""
        error = result.get("error") or ""
        self._update_url = result.get("url") or RELEASES_URL
        self.update_check_button.setEnabled(True)
        self.update_check_button.setText("Buscar actualizaciones")

        if status == "update":
            self._latest_update = result
            self.update_label.setText(f"Actualización disponible: v{latest}")
            self.update_button.setVisible(True)
            has_asset = bool(result.get("asset_url")) and can_self_update()
            self.update_button.setText("Actualizar" if has_asset else "Descargar")
            self._append_log(f"Actualización disponible: v{latest} — {self._update_url}")
            if has_asset and latest and latest != self._auto_prompted_version:
                self._auto_prompted_version = latest
                self._open_update_window()
        elif status == "current":
            self.update_label.setText(f"Estás al día (v{APP_VERSION})")
            self.update_button.setVisible(False)
            self._append_log(f"Sin actualizaciones: v{APP_VERSION} es la última versión.")
        else:
            self.update_label.setText("No se pudo comprobar si hay actualizaciones.")
            self.update_button.setVisible(False)
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
        self.worker.stop()
        self.tray_icon.hide()
        QApplication.quit()

    def _open_release_page(self):
        QDesktopServices.openUrl(QUrl(self._update_url))

    def _update_status(self, state: str, device_text: str, color_hex: str):
        self.status_dot.setStyleSheet(f"color: {color_hex}; font-size: 15px;")
        self.status_label.setText(state)
        self.device_label.setText(device_text)
        icon = self._create_app_icon(color_hex)
        self.setWindowIcon(icon)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip(
            f"SpeakerPhone-USB v{APP_VERSION} — {state}: {device_text}"
        )

    def _ui_rendering(self):
        """True solo si la ventana está visible, restaurada y en primer plano."""
        return (
            self.isVisible()
            and not self.isMinimized()
            and self.isActiveWindow()
        )

    def _update_meter(self, level: float, clipping: bool):
        self._pending_level = float(level)
        self._pending_clip = bool(clipping)
        if not self._ui_rendering():
            return
        self._render_meter()

    def _render_meter(self):
        self.vu_meter.setValue(int(self._pending_level))
        if self._badge_state != self._pending_clip:
            self._badge_state = self._pending_clip
            self._apply_clip_badge()

    @staticmethod
    def _repolish(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _apply_clip_badge(self):
        self.clip_badge.setText(
            "SATURACIÓN (CLIP)" if self._pending_clip else "SEÑAL OK"
        )
        self.clip_badge.setObjectName(
            "clipAlert" if self._pending_clip else "clipOk"
        )
        self.vu_meter.setObjectName(
            "vuClip" if self._pending_clip else "vuNormal"
        )
        self._repolish(self.clip_badge)
        self._repolish(self.vu_meter)

    def _refresh_rendered_ui(self):
        if self._ui_rendering():
            self._render_meter()

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

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()

    def closeEvent(self, event):
        self._save_settings()
        event.ignore()
        self.hide()

    def _quit_app(self):
        self._save_settings()
        self.worker.stop()
        self.tray_icon.hide()
        QApplication.quit()


if __name__ == "__main__":
    setup_logging()
    install_exception_hooks()
    log_startup_info()
    log(f"Interfaz Qt: PySide6 {getattr(PySide6, '__version__', 'desconocida')}")
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
