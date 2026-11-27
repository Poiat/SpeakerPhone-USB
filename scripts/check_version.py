#!/usr/bin/env python3
"""Comprueba que la versión de PC, la de Android y (opcional) el tag coincidan."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION  # noqa: E402
from protocol import HEADER_SIZE, STATUS_SIZE, VERSION  # noqa: E402


def android_version():
    source = ROOT / "android/app/src/main/java/com/usbaudio/MainActivity.kt"
    match = re.search(r'APP_VERSION\s*=\s*"v?([^"]+)"', source.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("No se encontró APP_VERSION en MainActivity.kt")
    return match.group(1)


def android_protocol():
    """Constantes del protocolo declaradas en AudioStreamService.kt."""
    source = ROOT / "android/app/src/main/java/com/usbaudio/AudioStreamService.kt"
    text = source.read_text(encoding="utf-8")
    values = {}
    for name in ("PROTOCOL_VERSION", "HEADER_SIZE", "STATUS_SIZE"):
        match = re.search(rf"\b{name}\s*=\s*(\d+)", text)
        if not match:
            raise SystemExit(f"No se encontró {name} en AudioStreamService.kt")
        values[name] = int(match.group(1))
    return values


def main():
    version = APP_VERSION
    android = android_version()
    protocol = android_protocol()
    problems = []
    if android != version:
        problems.append(f"PC={version} Android={android}")
    if protocol["PROTOCOL_VERSION"] != VERSION:
        problems.append(f"protocolo PC={VERSION} Android={protocol['PROTOCOL_VERSION']}")
    if protocol["HEADER_SIZE"] != HEADER_SIZE:
        problems.append(
            f"header PC={HEADER_SIZE} Android={protocol['HEADER_SIZE']}"
        )
    if protocol["STATUS_SIZE"] != STATUS_SIZE:
        problems.append(
            f"estado PC={STATUS_SIZE} Android={protocol['STATUS_SIZE']}"
        )

    if len(sys.argv) > 1:
        tag = sys.argv[1].lstrip("v")
        if tag != version:
            problems.append(f"tag={tag} PC={version}")

    if problems:
        raise SystemExit("Inconsistencias: " + ", ".join(problems))

    print(
        f"Versión consistente: {version} (Android {android}); "
        f"protocolo v{VERSION} (header {HEADER_SIZE}, estado {STATUS_SIZE})"
    )


if __name__ == "__main__":
    main()
