"""Aplica la paleta claro/oscuro compartida a las apps PySide6 de Windows/macOS."""

from PySide6.QtGui import QColor, QPalette

from theme import get_theme


def apply_theme(app, name):
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
    app.setStyleSheet(
        f"QToolTip {{ background-color: {palette['panel']}; "
        f"color: {palette['text']}; border: 1px solid {palette['border']}; }}"
    )
