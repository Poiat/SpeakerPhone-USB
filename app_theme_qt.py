"""Paleta y hoja de estilos compartida por las apps PySide6 de escritorio."""

from string import Template

from theme import get_theme


def build_stylesheet(name):
    """Devuelve el QSS común para las ventanas de Linux, Windows y macOS."""
    palette = get_theme(name)
    return Template(
        """
        QWidget {
            background-color: $bg;
            color: $text;
            font-family: sans-serif;
            font-size: 13px;
        }
        QMainWindow, QDialog {
            background-color: $bg;
        }
        QScrollArea {
            background-color: $bg;
            border: none;
        }
        QLabel {
            background-color: transparent;
        }
        QLabel#titleLabel {
            font-size: 19px;
            font-weight: 700;
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
        QFrame#card, QGroupBox {
            background-color: $panel;
            border: 1px solid $border;
            border-radius: 10px;
        }
        QGroupBox {
            margin-top: 10px;
            padding: 12px 10px 10px 10px;
            font-weight: 600;
            color: $value;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
            color: $section;
            font-size: 11px;
        }
        QFrame#sep {
            color: $border;
        }
        QLabel#sectionLabel {
            color: $section;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.6px;
        }
        QLabel#valueLabel {
            font-size: 14px;
            font-weight: 600;
            color: $value;
        }
        QLabel#hintLabel {
            color: $muted;
            font-size: 11px;
        }
        QLabel#clipOk, QLabel#adbOk {
            background-color: $ok_bg;
            color: $ok_fg;
            border: 1px solid $ok_border;
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 10px;
            font-weight: 700;
        }
        QLabel#clipAlert, QLabel#adbError {
            background-color: $error_bg;
            color: $error_fg;
            border: 1px solid $error_border;
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 10px;
            font-weight: 700;
        }
        QLabel#adbWarn {
            background-color: $warn_bg;
            color: $warn_fg;
            border: 1px solid $warn_border;
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 10px;
            font-weight: 700;
        }
        QLabel#adbNeutral {
            background-color: $neutral_bg;
            color: $neutral_text;
            border: 1px solid $neutral_border;
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 10px;
            font-weight: 700;
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
        QComboBox:hover, QSpinBox:hover {
            border-color: $accent_border;
        }
        QComboBox QAbstractItemView {
            background-color: $panel;
            color: $text;
            selection-background-color: $accent;
            selection-color: #ffffff;
            border: 1px solid $border;
        }
        QCheckBox {
            color: $neutral_text;
            spacing: 6px;
        }
        QSlider::groove:horizontal {
            height: 4px;
            background: $input_border;
            border-radius: 2px;
        }
        QSlider::sub-page:horizontal {
            background: $accent;
            border-radius: 2px;
        }
        QSlider::handle:horizontal {
            background: $accent;
            border: 1px solid $accent_border;
            width: 14px;
            height: 14px;
            margin: -6px 0;
            border-radius: 7px;
        }
        QSlider::handle:horizontal:hover {
            background: $accent_hover;
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
        QPushButton:pressed {
            background-color: $border;
        }
        QPushButton:disabled {
            color: $muted;
            background-color: $panel;
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
        QPushButton#miniBtn:checked {
            background-color: $error_bg;
            color: $error_fg;
            border: 1px solid $error_border;
        }
        QPlainTextEdit, QTextEdit {
            background-color: $log_bg;
            border: 1px solid $log_border;
            border-radius: 8px;
            color: $log_text;
            font-family: monospace;
            font-size: 11px;
            padding: 6px;
            selection-background-color: $accent;
            selection-color: #ffffff;
        }
        QScrollBar:vertical {
            background: $bg;
            width: 10px;
            margin: 0;
        }
        QScrollBar::handle:vertical {
            background: $border;
            min-height: 24px;
            border-radius: 5px;
        }
        QScrollBar::handle:vertical:hover {
            background: $button_hover;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: none;
            height: 0;
        }
        QScrollBar:horizontal {
            background: $bg;
            height: 10px;
            margin: 0;
        }
        QScrollBar::handle:horizontal {
            background: $border;
            min-width: 24px;
            border-radius: 5px;
        }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
            background: none;
            width: 0;
        }
        QToolTip {
            background-color: $panel;
            color: $text;
            border: 1px solid $border;
            padding: 4px 6px;
        }
        QMenu {
            background-color: $panel;
            color: $text;
            border: 1px solid $border;
        }
        QMenu::item:selected {
            background-color: $accent;
            color: #ffffff;
        }
        """
    ).substitute(palette)


def apply_theme(app, name):
    """Aplica la paleta y el QSS compartido a toda la aplicación."""
    from PySide6.QtGui import QColor, QPalette

    palette = get_theme(name)
    if name == "light":
        qpalette = app.style().standardPalette()
        qpalette.setColor(QPalette.ColorRole.Window, QColor(palette["bg"]))
        qpalette.setColor(QPalette.ColorRole.WindowText, QColor(palette["text"]))
        qpalette.setColor(QPalette.ColorRole.Base, QColor(palette["input_bg"]))
        qpalette.setColor(QPalette.ColorRole.AlternateBase, QColor(palette["panel"]))
        qpalette.setColor(QPalette.ColorRole.Text, QColor(palette["input_text"]))
        qpalette.setColor(QPalette.ColorRole.Button, QColor(palette["button_bg"]))
        qpalette.setColor(QPalette.ColorRole.ButtonText, QColor(palette["button_text"]))
        qpalette.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette["panel"]))
        qpalette.setColor(QPalette.ColorRole.ToolTipText, QColor(palette["text"]))
        qpalette.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette["muted"]))
    else:
        qpalette = QPalette()
        for role, key in (
            (QPalette.ColorRole.Window, "bg"),
            (QPalette.ColorRole.WindowText, "text"),
            (QPalette.ColorRole.Base, "input_bg"),
            (QPalette.ColorRole.AlternateBase, "panel"),
            (QPalette.ColorRole.Text, "input_text"),
            (QPalette.ColorRole.Button, "button_bg"),
            (QPalette.ColorRole.ButtonText, "button_text"),
            (QPalette.ColorRole.ToolTipBase, "panel"),
            (QPalette.ColorRole.ToolTipText, "text"),
            (QPalette.ColorRole.PlaceholderText, "muted"),
            (QPalette.ColorRole.BrightText, "error_fg"),
        ):
            qpalette.setColor(role, QColor(palette[key]))
        qpalette.setColor(QPalette.ColorRole.Highlight, QColor(palette["accent"]))
        qpalette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        qpalette.setColor(QPalette.ColorRole.Mid, QColor(palette["border"]))
        qpalette.setColor(QPalette.ColorRole.Dark, QColor(palette["border"]))
        qpalette.setColor(QPalette.ColorRole.Shadow, QColor("#000000"))
        qpalette.setColor(QPalette.ColorRole.Light, QColor(palette["button_hover"]))

    app.setPalette(qpalette)
    app.setStyleSheet(build_stylesheet(name))
