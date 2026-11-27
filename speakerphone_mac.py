import sys
import os
import re
import json
import time
import math
import shutil
import socket
import subprocess
import threading
import pyaudio

import PySide6
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSlider, QCheckBox, QTextEdit,
    QProgressBar, QSystemTrayIcon, QMenu, QGroupBox
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
from dsp import process_pcm_s16le
from protocol import pack_header, read_status_frame
from stream_tuning import auto_tune_target, format_stats
from update_checker import RELEASES_URL, check_for_update
from update_window_qt import UpdateWindow
from theme import valid_theme
from updater import can_self_update

DEFAULT_PORT = 5000
DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
MAX_LOG_LINES = 300
SEND_TIMEOUT_SECONDS = 5.0
SOCKET_BUFFER_BYTES = 32768


def get_adb_path() -> str:
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for p in [os.path.join(base_dir, "adb", "adb"), "/opt/homebrew/bin/adb", "/usr/local/bin/adb"]:
        if os.path.exists(p):
            return p
    return "adb"


def _run_adb_cmd(exe: str, args: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [exe] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
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
        "ADB no está instalado. Instálalo con: brew install android-platform-tools"
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
    stats_updated = Signal(int, int, int, int, bool)
    auto_tuned = Signal(int)


class AutoTuned(ConnectionError):
    """Se subió el búfer del PC y hay que reabrir la sesión."""


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
        self.gain_linear = 1.0
        self.soft_limiter = True
        self._lock = threading.Lock()
        self._sock = None
        self._stream = None
        self._auto_tune_ms = None
        self._auto_tune_to = None
        self._last_auto_tune_at = 0.0
        self._pa = pyaudio.PyAudio()

    def set_device(self, device_index: int):
        with self._lock:
            if self.device_index != device_index:
                self.device_index = device_index
                self._auto_tune_ms = None
                self._auto_tune_to = None
                self.force_reconnect = True

    def set_chunk_ms(self, ms: int):
        with self._lock:
            if self.chunk_ms != ms:
                self.chunk_ms = ms
                self._auto_tune_ms = None
                self._auto_tune_to = None
                self.force_reconnect = True

    def set_gain_db(self, db: float):
        self.gain_linear = math.pow(10.0, db / 20.0)

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

    def _status_reader(self, sock):
        """Lee la telemetría del móvil y decide si el PC debe subir su búfer."""
        last_underruns = None
        streak = 0
        while self.running and self._sock is sock:
            try:
                frame = read_status_frame(sock)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                return
            if frame is None:
                return

            underruns = int(frame["underruns"])
            delta = (
                0
                if last_underruns is None
                else max(0, underruns - last_underruns)
            )
            last_underruns = underruns
            streak = streak + 1 if delta > 0 else 0
            now = time.monotonic()
            with self._lock:
                current = self._auto_tune_ms or self.chunk_ms
                auto_active = self._auto_tune_ms is not None
            target = auto_tune_target(
                current,
                frame["buffer_ms"],
                delta,
                streak,
                now - self._last_auto_tune_at,
            )
            if target and self._auto_tune_to is None:
                with self._lock:
                    if self._auto_tune_to is None:
                        self._auto_tune_to = target
                self._last_auto_tune_at = now
                streak = 0
            self.signals.stats_updated.emit(
                underruns,
                frame["buffer_ms"],
                frame["backlog_ms"],
                current,
                auto_active,
            )

    def run(self):
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
                chunk_ms = self._auto_tune_ms or self.chunk_ms
                self._auto_tune_to = None

            if dev_idx is None:
                time.sleep(0.5)
                continue

            try:
                dev_info = self._pa.get_device_info_by_index(dev_idx)
                rate = int(dev_info.get("defaultSampleRate", DEFAULT_RATE))
                ch = max(1, min(2, int(dev_info.get("maxInputChannels", DEFAULT_CHANNELS))))
                frames = max(128, int(rate * chunk_ms / 1000))

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
                sock.sendall(pack_header(rate, ch, chunk_ms))
                threading.Thread(
                    target=self._status_reader, args=(sock,), daemon=True
                ).start()

                self._stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=ch,
                    rate=rate,
                    input=True,
                    input_device_index=dev_idx,
                    frames_per_buffer=frames
                )

                dev_title = dev_info["name"]
                self.signals.status_changed.emit("Transmitiendo", f"{info} ({rate} Hz)", "#2ECC71")
                if last_state != f"streaming_{info}_{dev_idx}_{chunk_ms}":
                    self.signals.log.emit(f"[Audio] Transmitiendo {dev_title} ({rate}Hz, {ch}ch, {chunk_ms}ms) -> tcp:{self.tcp_port}")
                    last_state = f"streaming_{info}_{dev_idx}_{chunk_ms}"

                last_ui = 0.0
                while self.running and not self.force_reconnect and not self.paused:
                    raw_data = self._stream.read(frames, exception_on_overflow=False)
                    out_bytes, lvl, clip = self._process_audio(raw_data)
                    try:
                        self._sock.sendall(out_bytes)
                    except socket.timeout as exc:
                        raise ConnectionError(
                            "El móvil dejó de leer audio durante "
                            f"{SEND_TIMEOUT_SECONDS:.0f} s; reconectando"
                        ) from exc
                    now = time.monotonic()
                    if (now - last_ui) >= 0.06:
                        self.signals.level_updated.emit(lvl, clip)
                        last_ui = now

                    with self._lock:
                        auto_target = self._auto_tune_to
                        if auto_target:
                            self._auto_tune_to = None
                            self._auto_tune_ms = auto_target
                    if auto_target:
                        self.signals.log.emit(
                            f"[auto] Búfer del PC subido a {auto_target} ms por "
                            "underruns del móvil; se mantiene hasta reiniciar."
                        )
                        self.signals.auto_tuned.emit(auto_target)
                        raise AutoTuned(
                            f"Ajuste automático de búfer a {auto_target} ms"
                        )

            except AutoTuned:
                self._close_connections()
                self.signals.level_updated.emit(0.0, False)
                time.sleep(0.4)
            except (ConnectionRefusedError, ConnectionError, socket.timeout, BrokenPipeError, OSError) as e:
                if last_state != "waiting_app":
                    self.signals.status_changed.emit("Reconectando...", info, "#F39C12")
                    self.signals.log.emit(f"[Stream] Esperando app Android en puerto {self.tcp_port} ({e})")
                    last_state = "waiting_app"
                self._close_connections()
                self.signals.level_updated.emit(0.0, False)
                time.sleep(1.2)
            except Exception as e:
                self.signals.log.emit(f"[Error] {e}")
                self._close_connections()
                time.sleep(1.5)
            finally:
                self._close_connections()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SpeakerPhone-USB v{APP_VERSION} (macOS)")
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
            painter.setFont(QFont("Helvetica Neue", 24, QFont.Bold))
            painter.drawText(pixmap.rect(), Qt.AlignCenter, "USB")

        painter.setPen(QColor("#0f172a"))
        painter.setBrush(QColor(color_hex))
        painter.drawEllipse(42, 42, 18, 18)
        painter.end()
        return QIcon(pixmap)

    def _init_ui(self):
        self.setWindowIcon(self._create_app_icon())
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        status_group = QGroupBox("Estado de Conexion USB / ADB")
        status_vbox = QVBoxLayout(status_group)

        status_layout = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #E74C3C; font-size: 18px;")
        self.status_label = QLabel("Iniciando...")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        self.device_label = QLabel("Buscando dispositivo...")
        self.btn_retry = QPushButton("Reintentar")
        self.btn_toggle = QPushButton("Detener")
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.device_label, 1)
        status_layout.addWidget(self.btn_retry)
        status_layout.addWidget(self.btn_toggle)
        status_vbox.addLayout(status_layout)

        adb_row = QHBoxLayout()
        self.adb_badge = QLabel("ADB: comprobando...")
        self.adb_badge.setStyleSheet("color: #95A5A6; font-weight: bold; font-size: 11px;")
        self.adb_detail = QLabel("Verificando si adb está instalado y responde...")
        self.adb_detail.setStyleSheet("color: #7F8C8D; font-size: 11px;")
        self.adb_detail.setWordWrap(True)
        self.btn_adb_check = QPushButton("↻")
        self.btn_adb_check.setFixedWidth(32)
        self.btn_adb_check.setToolTip("Volver a comprobar si adb está instalado y funciona")
        adb_row.addWidget(self.adb_badge)
        adb_row.addWidget(self.adb_detail, 1)
        adb_row.addWidget(self.btn_adb_check)
        status_vbox.addLayout(adb_row)

        update_row = QHBoxLayout()
        self.update_label = QLabel("Buscando actualizaciones...")
        self.update_label.setStyleSheet("color: #7F8C8D; font-size: 11px;")
        self.update_label.setWordWrap(True)
        self.update_check_button = QPushButton("Buscar actualizaciones")
        self.update_check_button.setToolTip("Volver a comprobar si hay una versión nueva")
        self.update_check_button.clicked.connect(self._check_updates)
        self.update_button = QPushButton("Descargar")
        self.update_button.setVisible(False)
        self.update_button.clicked.connect(self._open_update_window)
        update_row.addWidget(self.update_label, 1)
        update_row.addWidget(self.update_check_button)
        update_row.addWidget(self.update_button)
        status_vbox.addLayout(update_row)

        layout.addWidget(status_group)

        audio_group = QGroupBox("Fuente de Audio, Ganancia y Latencia (CoreAudio)")
        audio_layout = QVBoxLayout(audio_group)

        row_source = QHBoxLayout()
        row_source.addWidget(QLabel("Fuente:"))
        self.combo_devices = QComboBox()
        self.btn_refresh_dev = QPushButton("↻")
        self.btn_refresh_dev.setFixedWidth(32)
        row_source.addWidget(self.combo_devices, 1)
        row_source.addWidget(self.btn_refresh_dev)
        audio_layout.addLayout(row_source)

        row_gain = QHBoxLayout()
        row_gain.addWidget(QLabel("Ganancia:"))
        self.slider_gain = QSlider(Qt.Horizontal)
        self.slider_gain.setRange(-120, 120)
        self.slider_gain.setValue(0)
        self.label_gain = QLabel("0.0 dB")
        self.label_gain.setFixedWidth(52)
        self.chk_limiter = QCheckBox("Limitador suave")
        self.chk_limiter.setChecked(True)
        row_gain.addWidget(self.slider_gain, 1)
        row_gain.addWidget(self.label_gain)
        row_gain.addWidget(self.chk_limiter)
        audio_layout.addLayout(row_gain)

        row_meter = QHBoxLayout()
        row_meter.addWidget(QLabel("Nivel:"))
        self.vu_meter = QProgressBar()
        self.vu_meter.setRange(0, 100)
        self.vu_meter.setTextVisible(False)
        self.vu_meter.setFixedHeight(14)
        self.clip_badge = QLabel("CLIP")
        self.clip_badge.setAlignment(Qt.AlignCenter)
        self.clip_badge.setFixedWidth(48)
        self.clip_badge.setStyleSheet("background-color: #2C3E50; color: #7F8C8D; font-weight: bold; border-radius: 4px; padding: 2px;")
        row_meter.addWidget(self.vu_meter, 1)
        row_meter.addWidget(self.clip_badge)
        audio_layout.addLayout(row_meter)

        self.stats_label = QLabel("Móvil: esperando telemetría…")
        self.stats_label.setObjectName("hintLabel")
        self.stats_label.setWordWrap(True)
        self.stats_label.setAccessibleName("Telemetría del móvil")
        audio_layout.addWidget(self.stats_label)

        row_latency = QHBoxLayout()
        row_latency.addWidget(QLabel("Perfil de búfer:"))
        self.combo_profile = QComboBox()
        self.combo_profile.addItem("Baja latencia (8 ms)", ("low", 8))
        self.combo_profile.addItem("Equilibrado (15 ms)", ("balanced", 15))
        self.combo_profile.addItem("Alta estabilidad (30 ms)", ("stable", 30))
        self.combo_profile.addItem("Máxima estabilidad (60 ms)", ("max_stable", 60))
        self.combo_profile.setItemData(0, "Mínima latencia (juegos)", Qt.ItemDataRole.ToolTipRole)
        self.combo_profile.setItemData(1, "Balance latencia/estabilidad", Qt.ItemDataRole.ToolTipRole)
        self.combo_profile.setItemData(2, "Mayor margen contra cortes USB", Qt.ItemDataRole.ToolTipRole)
        self.combo_profile.setItemData(3, "Resiste picos de CPU y bloqueos breves", Qt.ItemDataRole.ToolTipRole)
        self.combo_profile.setCurrentIndex(1)
        row_latency.addWidget(self.combo_profile, 1)
        audio_layout.addLayout(row_latency)
        layout.addWidget(audio_group)

        log_group = QGroupBox("Registros en Vivo")
        log_layout = QVBoxLayout(log_group)
        log_header = QHBoxLayout()
        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setStyleSheet("color: #95A5A6; font-size: 11px; font-weight: bold;")
        self.theme_button = QPushButton("☀ Claro")
        self.theme_button.setToolTip("Cambiar entre tema oscuro y claro")
        self.theme_button.setAccessibleName("Cambiar tema")
        self.theme_button.clicked.connect(self._toggle_theme)
        btn_open_logs = QPushButton("Abrir carpeta de logs")
        btn_open_logs.setToolTip(get_log_path() or "Carpeta de logs no disponible")
        btn_open_logs.clicked.connect(self._open_log_folder)
        log_header.addWidget(self.version_label)
        log_header.addWidget(self.theme_button)
        log_header.addStretch(1)
        log_header.addWidget(btn_open_logs)
        log_layout.addLayout(log_header)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setFont(QFont("Menlo", 10))
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, 1)

    def _init_tray(self):
        self.tray_icon = QSystemTrayIcon(self._create_app_icon("#E74C3C"), self)
        tray_menu = QMenu()
        a_show = QAction("Mostrar / Ocultar", self)
        a_show.triggered.connect(self._toggle_visibility)
        a_rec = QAction("Reconectar USB", self)
        a_rec.triggered.connect(self.worker.trigger_reconnect)
        a_quit = QAction("Salir", self)
        a_quit.triggered.connect(self._quit_app)
        tray_menu.addAction(a_show)
        tray_menu.addAction(a_rec)
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
        self.signals.stats_updated.connect(self._update_stats)
        self.signals.auto_tuned.connect(self._on_auto_tuned)
        self.btn_retry.clicked.connect(self._on_retry)
        self.btn_toggle.clicked.connect(self._on_toggle)
        self.btn_adb_check.clicked.connect(self._refresh_adb_status)
        self.btn_refresh_dev.clicked.connect(self._populate_audio_devices)
        self.combo_devices.currentIndexChanged.connect(self._on_device_changed)
        self.slider_gain.valueChanged.connect(self._on_gain_changed)
        self.chk_limiter.toggled.connect(self._on_limiter_toggled)
        self.combo_profile.currentIndexChanged.connect(self._on_profile_changed)

    def _populate_audio_devices(self):
        self.combo_devices.blockSignals(True)
        self.combo_devices.clear()
        pa = self.worker._pa
        try:
            coreaudio_idx = pa.get_host_api_info_by_type(pyaudio.paCoreAudio)["index"]
        except OSError:
            coreaudio_idx = None
        default_idx = 0
        count = 0
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if coreaudio_idx is not None and dev["hostApi"] != coreaudio_idx:
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
                "soft_limiter": self.chk_limiter.isChecked(),
                "profile": profile_data[0] if profile_data else "",
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
        self.vu_meter.setAccessibleName("Nivel de señal")
        self.clip_badge.setAccessibleName("Estado de saturación")
        self.stats_label.setAccessibleName("Telemetría del móvil")
        self.btn_retry.setAccessibleName("Reintentar conexión")
        self.btn_toggle.setAccessibleName("Iniciar o detener transmisión")
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

    def _on_profile_changed(self, idx: int):
        data = self.combo_profile.itemData(idx)
        if data:
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

    def _open_log_folder(self):
        if not open_log_folder():
            self._append_log("No se pudo abrir la carpeta de logs.")

    def _refresh_adb_status(self):
        """Lanza la comprobación de ADB en segundo plano para no bloquear la UI."""
        self.adb_badge.setText("ADB: comprobando...")
        self.adb_badge.setStyleSheet("color: #95A5A6; font-weight: bold; font-size: 11px;")
        self.adb_detail.setText("Verificando si adb está instalado y responde...")
        threading.Thread(target=self._adb_check_worker, daemon=True).start()

    def _adb_check_worker(self):
        state, detail = check_adb_health()
        self.signals.adb_checked.emit(state, detail)

    def _on_adb_checked(self, state: str, detail: str):
        styles = {
            "ok": ("ADB OK", "#2ECC71"),
            "missing": ("ADB NO INSTALADO", "#E74C3C"),
            "error": ("ADB CON ERROR", "#F39C12"),
        }
        text, color = styles.get(state, ("ADB: ?", "#95A5A6"))
        self.adb_badge.setText(text)
        self.adb_badge.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 11px;")
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
        self.status_dot.setStyleSheet(f"color: {color_hex}; font-size: 18px;")
        self.status_label.setText(state)
        self.device_label.setText(device_text)
        icon = self._create_app_icon(color_hex)
        self.setWindowIcon(icon)
        self.tray_icon.setIcon(icon)
        if state != "Transmitiendo":
            self.stats_label.setText("Móvil: esperando telemetría…")

    def _update_stats(self, underruns, buffer_ms, backlog_ms, pc_ms, auto):
        self.stats_label.setText(
            format_stats(
                pc_ms, buffer_ms, backlog_ms, underruns, auto=bool(auto)
            )
        )

    def _on_auto_tuned(self, target_ms):
        self.stats_label.setText(
            f"Ajuste automático: búfer del PC a {int(target_ms)} ms (temporal)"
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

    def _apply_clip_badge(self):
        if self._pending_clip:
            bg, fg = "#E74C3C", "#FFFFFF"
        else:
            bg, fg = "#2C3E50", "#7F8C8D"
        self.clip_badge.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-weight: bold; "
            "border-radius: 4px; padding: 2px;"
        )

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
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
