#!/usr/bin/env python3
"""Comprueba que la versión de PC, la de Android y (opcional) el tag coincidan."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION  # noqa: E402


def android_version():
    source = ROOT / "android/app/src/main/java/com/usbaudio/MainActivity.kt"
    match = re.search(r'APP_VERSION\s*=\s*"v?([^"]+)"', source.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("No se encontró APP_VERSION en MainActivity.kt")
    return match.group(1)


def main():
    version = APP_VERSION
    android = android_version()
    problems = []
    if android != version:
        problems.append(f"PC={version} Android={android}")

    if len(sys.argv) > 1:
        tag = sys.argv[1].lstrip("v")
        if tag != version:
            problems.append(f"tag={tag} PC={version}")

    if problems:
        raise SystemExit("Versiones inconsistentes: " + ", ".join(problems))

    print(f"Versión consistente: {version} (Android {android})")


if __name__ == "__main__":
    main()
