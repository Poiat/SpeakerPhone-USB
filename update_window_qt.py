"""Ventana de actualización en vivo para las apps PySide6 de Windows/macOS."""

import threading
from string import Template

from PySide6.QtCore import Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from theme import get_theme
from updater import UpdateCancelled, apply_update, download_update


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
            self.detail.setText(f"{written / 1048576:.1f} / {total / 1048576:.1f} MB")
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
