# Contribuir a SpeakerPhone-USB

¡Gracias por querer mejorar el proyecto! Esta guía resume el flujo de trabajo.

## Entorno

- Python 3.11 para builds de PyInstaller; 3.12+ también sirve para desarrollo.
- Dependencias de runtime: `pip install -r requirements.txt`
- Herramientas de desarrollo: `pip install -r requirements-dev.txt`
- Build de escritorio: `pip install -r requirements-build.txt`

## Antes de enviar cambios

```bash
python scripts/check_version.py
python -m pytest -q
ruff check --select E9,F app_settings.py app_icon.py app_version.py dsp.py \
  protocol.py update_checker.py updater.py update_window_qt.py theme.py \
  app_theme_qt.py scripts tests
python -m pip check
```

- No subas artefactos generados (`build/`, `dist/`, `android/app/build/`,
  `.gradle/`, `.idea/`, `local.properties`, AppImages). El `.gitignore` ya los
  excluye; usa `git status` para comprobarlo.
- Mantén las dependencias fijadas en `requirements*.txt`. Dependabot propone
  actualizaciones; no las aceptes sin ejecutar tests y `pip-audit`.
- No actualices PySide6 por encima de 6.11.x sin comprobar el bug de
  `Signal.emit()`/`bool_dealloc` en Python < 3.12.

## Versiones y releases

1. Actualiza `app_version.py` y `APP_VERSION` en
   `android/app/src/main/java/com/usbaudio/MainActivity.kt`.
2. Actualiza `CHANGELOG.md` y las notas del release en
   `.github/workflows/release.yml`.
3. Ejecuta `python scripts/check_version.py <tag>`.
4. Crea un tag numérico (por ejemplo `0.22`) y súbelo. El workflow construye
   los binarios, el SBOM y los checksums, y publica el release.

Los cambios de protocolo (`protocol.py` y `AudioStreamService.kt`) deben
actualizar **ambas partes** y anotarse en el CHANGELOG. PC y Android deben
usar la misma versión de protocolo.

## Firma

El APK de release requiere los secrets `ANDROID_KEYSTORE_BASE64`,
`ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` y `ANDROID_KEY_PASSWORD`.
Sin ellos el CI publica un APK debug con un aviso y no sirve para
actualizaciones in-place.

Windows/macOS aún no están firmados; si añades firma, documenta los secrets y
los pasos en `README.md`.

## Pull requests

- Un cambio por PR, con una descripción clara del problema y la solución.
- Incluye tests cuando toques DSP, protocolo, actualizaciones o ajustes.
- Indica si probaste Linux/Windows/macOS/Android y con qué dispositivos.
- No mezcles refactors grandes con cambios funcionales.

Al enviar un PR aceptas que tu contribución se publique bajo la licencia MIT
del proyecto.
