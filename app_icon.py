"""Localiza el icono de la aplicación tanto desde el código fuente como empaquetado."""

import os
import sys

ICON_FILENAME = "foid.png"


def get_icon_path():
    """
    Devuelve la ruta de foid.png o None si no se encuentra.

    Cubre ejecución desde fuente, PyInstaller (onefile/onedir) y el caso de
    tener el PNG junto al ejecutable.
    """
    candidates = []

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(os.path.join(bundle_dir, ICON_FILENAME))

    candidates.append(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ICON_FILENAME)
    )

    if getattr(sys, "frozen", False):
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(sys.executable)), ICON_FILENAME)
        )

    candidates.append(os.path.join(os.getcwd(), ICON_FILENAME))

    for path in candidates:
        if os.path.exists(path):
            return path
    return None
