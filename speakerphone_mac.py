import sys
import os
import re
import time
import math
import shutil
import socket
import struct
import subprocess
import threading
import numpy as np
import pyaudio

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSlider, QCheckBox, QTextEdit,
    QProgressBar, QSystemTrayIcon, QMenu, QGroupBox
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QFont

from app_logger import (
    get_log_path,
    install_exception_hooks,
    log,
    log_startup_info,
    open_log_folder,
    setup_logging,
)

DEFAULT_PORT = 5000
DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 2
CLIP_THRESHOLD = 0.98


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
        for line in out.splitlines()[1:]:
            parts = line.strip().split("	")
            if len(parts) == 2:
                serial, state = parts[0], parts[1]
                if state == "device":
                    _, model = self._run_adb(["-s", serial, "shell", "getprop", "ro.product.model"])
                    return "device", f"{model} ({serial})" if model else serial
                return state, serial
        return "none", None

    def _setup_adb_forward(self, serial_info: str) -> bool:
        serial = serial_info.split("(")[-1].rstrip(")") if "(" in serial_info else serial_info
        code, out = self._run_adb(["-s", serial, "forward", f"tcp:{self.tcp_port}", f"tcp:{self.tcp_port}"])
        if code != 0:
            self.signals.log.emit(f"[ADB] Error en port-forward: {out}")
            return False
        return True

    def _process_audio(self, raw_data: bytes) -> tuple[bytes, float, bool]:
        samples = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return raw_data, 0.0, False

        g = self.gain_linear
        if abs(g - 1.0) > 1e-4 or self.soft_limiter:
            norm = (samples / 32768.0) * g
            if self.soft_limiter:
                norm = np.where(np.abs(norm) > 0.75, np.tanh(norm), norm)
            clipping = bool(np.max(np.abs(norm)) >= CLIP_THRESHOLD)
            clamped = np.clip(norm * 32767.0, -32768.0, 32767.0).astype(np.int16)
            out_bytes = clamped.tobytes()
            peak = float(np.max(np.abs(clamped))) / 32768.0
        else:
            out_bytes = raw_data
            peak = float(np.max(np.abs(samples))) / 32768.0
            clipping = peak >= CLIP_THRESHOLD

        return out_bytes, min(100.0, peak * 100.0), clipping

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
                chunk_ms = self.chunk_ms

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
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
                self._sock = sock

                ack = sock.recv(1)
                if ack != b"R":
                    raise ConnectionError("La app Android no respondio R")

                sock.settimeout(None)
                sock.sendall(struct.pack(">IBB", rate, ch, 16))

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
                    self._sock.sendall(out_bytes)
                    now = time.monotonic()
                    if (now - last_ui) >= 0.06:
                        self.signals.level_updated.emit(lvl, clip)
                        last_ui = now

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
        self.setWindowTitle("SpeakerPhone-USB v4.0 (macOS)")
        self.setMinimumSize(540, 520)
        self.signals = WorkerSignals()
        self.worker = AudioStreamWorker(self.signals)
        self._init_ui()
        self._init_tray()
        self._connect_signals()
        self._populate_audio_devices()
        self._refresh_adb_status()
        self.worker.start()

    def _create_app_icon(self, color_hex="#2ECC71") -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(color_hex))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(6, 6, 52, 52, 14, 14)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(QFont("Helvetica Neue", 24, QFont.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "USB")
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

        row_latency = QHBoxLayout()
        row_latency.addWidget(QLabel("Paquete USB:"))
        self.slider_latency = QSlider(Qt.Horizontal)
        self.slider_latency.setRange(5, 40)
        self.slider_latency.setValue(15)
        self.label_latency = QLabel("15 ms")
        self.label_latency.setFixedWidth(48)
        row_latency.addWidget(self.slider_latency, 1)
        row_latency.addWidget(self.label_latency)
        audio_layout.addLayout(row_latency)
        layout.addWidget(audio_group)

        log_group = QGroupBox("Registros en Vivo")
        log_layout = QVBoxLayout(log_group)
        log_header = QHBoxLayout()
        btn_open_logs = QPushButton("Abrir carpeta de logs")
        btn_open_logs.setToolTip(get_log_path() or "Carpeta de logs no disponible")
        btn_open_logs.clicked.connect(self._open_log_folder)
        log_header.addStretch(1)
        log_header.addWidget(btn_open_logs)
        log_layout.addLayout(log_header)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
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
        self.btn_retry.clicked.connect(self._on_retry)
        self.btn_toggle.clicked.connect(self._on_toggle)
        self.btn_adb_check.clicked.connect(self._refresh_adb_status)
        self.btn_refresh_dev.clicked.connect(self._populate_audio_devices)
        self.combo_devices.currentIndexChanged.connect(self._on_device_changed)
        self.slider_gain.valueChanged.connect(self._on_gain_changed)
        self.chk_limiter.toggled.connect(self.worker.set_soft_limiter)
        self.slider_latency.valueChanged.connect(self._on_latency_changed)

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
            self.combo_devices.setCurrentIndex(default_idx)
            self._on_device_changed(default_idx)

    def _on_device_changed(self, combo_idx: int):
        dev_idx = self.combo_devices.itemData(combo_idx)
        if dev_idx is not None:
            self.worker.set_device(dev_idx)

    def _on_gain_changed(self, val: int):
        db = val / 10.0
        self.label_gain.setText(f"{db:+.1f} dB")
        self.worker.set_gain_db(db)

    def _on_latency_changed(self, ms: int):
        self.label_latency.setText(f"{ms} ms")
        self.worker.set_chunk_ms(ms)

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

    def _update_status(self, state: str, device_text: str, color_hex: str):
        self.status_dot.setStyleSheet(f"color: {color_hex}; font-size: 18px;")
        self.status_label.setText(state)
        self.device_label.setText(device_text)
        icon = self._create_app_icon(color_hex)
        self.setWindowIcon(icon)
        self.tray_icon.setIcon(icon)

    def _update_meter(self, level: float, clipping: bool):
        self.vu_meter.setValue(int(level))
        bg = "#E74C3C" if clipping else "#2C3E50"
        fg = "#FFFFFF" if clipping else "#7F8C8D"
        self.clip_badge.setStyleSheet(f"background-color: {bg}; color: {fg}; font-weight: bold; border-radius: 4px; padding: 2px;")

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _quit_app(self):
        self.worker.stop()
        self.tray_icon.hide()
        QApplication.quit()


if __name__ == "__main__":
    setup_logging()
    install_exception_hooks()
    log_startup_info()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
