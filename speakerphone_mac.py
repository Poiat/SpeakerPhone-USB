import sys
import os
import time
import socket
import subprocess
import threading
import numpy as np
import pyaudio

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSlider, QTextEdit, QProgressBar,
    QSystemTrayIcon, QMenu, QGroupBox
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction, QFont


def get_adb_path() -> str:
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base_dir, "adb", "adb"),
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return "adb"


class WorkerSignals(QObject):
    log = Signal(str)
    status_changed = Signal(str, str, str)
    level_updated = Signal(float, bool)


class AudioStreamWorker(threading.Thread):
    def __init__(self, signals: WorkerSignals):
        super().__init__(daemon=True)
        self.signals = signals
        self.running = True
        self.force_reconnect = False
        self.device_index = None
        self.chunk_frames = 512
        self.tcp_port = 28200
        self._lock = threading.Lock()
        self._sock = None
        self._stream = None
        self._pa = pyaudio.PyAudio()

    def set_device(self, device_index: int):
        with self._lock:
            if self.device_index != device_index:
                self.device_index = device_index
                self.force_reconnect = True

    def set_latency_frames(self, frames: int):
        with self._lock:
            if self.chunk_frames != frames:
                self.chunk_frames = frames
                self.force_reconnect = True

    def trigger_reconnect(self):
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

    def _check_android_device(self) -> str | None:
        code, out = self._run_adb(["devices"])
        if code != 0:
            return None
        for line in out.splitlines()[1:]:
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1] == "device":
                serial = parts[0]
                _, model = self._run_adb(["-s", serial, "shell", "getprop", "ro.product.model"])
                return f"{model} ({serial})" if model else serial
        return None

    def _setup_adb_forward(self, serial_info: str) -> bool:
        serial = serial_info.split("(")[-1].rstrip(")") if "(" in serial_info else serial_info
        code, out = self._run_adb(["-s", serial, "forward", f"tcp:{self.tcp_port}", f"tcp:{self.tcp_port}"])
        if code != 0:
            self.signals.log.emit(f"[ADB] Error en port-forward: {out}")
            return False
        return True

    def run(self):
        last_state = ""
        while self.running:
            self.force_reconnect = False
            device_name = self._check_android_device()
            if not device_name:
                if last_state != "disconnected":
                    self.signals.status_changed.emit("Desconectado", "Ningun dispositivo ADB", "#E74C3C")
                    self.signals.log.emit("[ADB] Esperando dispositivo Android por USB...")
                    last_state = "disconnected"
                self.signals.level_updated.emit(0.0, False)
                time.sleep(1.5)
                continue

            if not self._setup_adb_forward(device_name):
                time.sleep(1.5)
                continue

            with self._lock:
                dev_idx = self.device_index
                frames = self.chunk_frames

            if dev_idx is None:
                time.sleep(0.5)
                continue

            try:
                dev_info = self._pa.get_device_info_by_index(dev_idx)
                rate = int(dev_info.get("defaultSampleRate", 48000))
                ch = max(1, min(2, int(dev_info.get("maxInputChannels", 2))))

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect(("127.0.0.1", self.tcp_port))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = sock

                self._stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=ch,
                    rate=rate,
                    input=True,
                    input_device_index=dev_idx,
                    frames_per_buffer=frames
                )

                self.signals.status_changed.emit("Conectado", device_name, "#2ECC71")
                if last_state != f"connected_{device_name}_{dev_idx}":
                    name = dev_info["name"]
                    self.signals.log.emit(f"[Audio] Transmitiendo {name} ({rate}Hz, {ch}ch) -> {device_name}")
                    last_state = f"connected_{device_name}_{dev_idx}"

                while self.running and not self.force_reconnect:
                    raw_data = self._stream.read(frames, exception_on_overflow=False)
                    samples = np.frombuffer(raw_data, dtype=np.int16)
                    if samples.size > 0:
                        peak = np.max(np.abs(samples))
                        clipping = bool(peak >= 32600)
                        rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))
                        level_pct = min(100.0, (rms / 32768.0) * 100.0 * 3.5)
                        self.signals.level_updated.emit(level_pct, clipping)
                    self._sock.sendall(raw_data)

            except (ConnectionRefusedError, socket.timeout, BrokenPipeError, OSError) as e:
                if last_state != "waiting_app":
                    self.signals.status_changed.emit("Reconectando...", device_name, "#F39C12")
                    self.signals.log.emit(f"[Stream] Reconexion silenciosa ({type(e).__name__})...")
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
        self.setMinimumSize(540, 460)
        self.signals = WorkerSignals()
        self.worker = AudioStreamWorker(self.signals)
        self._init_ui()
        self._init_tray()
        self._connect_signals()
        self._populate_audio_devices()
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
        status_layout = QHBoxLayout(status_group)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #E74C3C; font-size: 18px;")
        self.status_label = QLabel("Iniciando...")
        self.status_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        self.device_label = QLabel("Buscando dispositivo...")
        self.btn_retry = QPushButton("Reintentar Conexion")
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.device_label, 1)
        status_layout.addWidget(self.btn_retry)
        layout.addWidget(status_group)

        audio_group = QGroupBox("Fuente de Audio y Latencia (CoreAudio)")
        audio_layout = QVBoxLayout(audio_group)
        row_source = QHBoxLayout()
        row_source.addWidget(QLabel("Fuente de audio:"))
        self.combo_devices = QComboBox()
        self.btn_refresh_dev = QPushButton("↻")
        self.btn_refresh_dev.setFixedWidth(32)
        row_source.addWidget(self.combo_devices, 1)
        row_source.addWidget(self.btn_refresh_dev)
        audio_layout.addLayout(row_source)

        row_meter = QHBoxLayout()
        row_meter.addWidget(QLabel("Nivel de senal:"))
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
        row_latency.addWidget(QLabel("Tamano de bufer:"))
        self.slider_latency = QSlider(Qt.Horizontal)
        self.slider_latency.setRange(1, 8)
        self.slider_latency.setValue(2)
        self.label_latency = QLabel("512 frames (~10.7 ms)")
        row_latency.addWidget(self.slider_latency, 1)
        row_latency.addWidget(self.label_latency)
        audio_layout.addLayout(row_latency)
        layout.addWidget(audio_group)

        log_group = QGroupBox("Registros en Vivo")
        log_layout = QVBoxLayout(log_group)
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
        self.signals.log.connect(lambda m: self.log_view.append(f"[{time.strftime('%H:%M:%S')}] {m}"))
        self.signals.status_changed.connect(self._update_status)
        self.signals.level_updated.connect(self._update_meter)
        self.btn_retry.clicked.connect(self.worker.trigger_reconnect)
        self.btn_refresh_dev.clicked.connect(self._populate_audio_devices)
        self.combo_devices.currentIndexChanged.connect(self._on_device_changed)
        self.slider_latency.valueChanged.connect(self._on_latency_changed)

    def _populate_audio_devices(self):
        self.combo_devices.blockSignals(True)
        self.combo_devices.clear()
        pa = self.worker._pa
        default_idx = 0
        count = 0
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev["maxInputChannels"] > 0:
                name = dev["name"]
                is_virtual = "blackhole" in name.lower() or "loopback" in name.lower()
                prefix = "[Sistema / Virtual]" if is_virtual else "[Entrada]"
                self.combo_devices.addItem(f"{prefix} {name}", userData=i)
                if is_virtual and default_idx == 0:
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

    def _on_latency_changed(self, step: int):
        frames = step * 256
        ms = (frames / 48000.0) * 1000.0
        self.label_latency.setText(f"{frames} frames (~{ms:.1f} ms)")
        self.worker.set_latency_frames(frames)

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
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
