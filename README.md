# SpeakerPhone-USB

[![CI](https://github.com/Poiat/SpeakerPhone-USB/actions/workflows/ci.yml/badge.svg)](https://github.com/Poiat/SpeakerPhone-USB/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Poiat/SpeakerPhone-USB)](https://github.com/Poiat/SpeakerPhone-USB/releases)
[![License: MIT](https://img.shields.io/github/license/Poiat/SpeakerPhone-USB)](LICENSE)

Transmite el audio de tu computadora hacia tu teléfono Android en tiempo real a través de un cable USB (mediante el túnel TCP de **ADB**), permitiendo usar los altavoces o audífonos conectados al móvil como salida de audio de baja latencia sin depender de Bluetooth ni Wi-Fi.

---

## Características principales

* **Multiplataforma:** Ejecutables portátiles independientes para **Linux (x86_64)**, **Windows (x86_64)** y **macOS (Apple Silicon arm64)**.
* **Salida de audio nativa por sistema:**
  * **Linux:** Integración directa con **PipeWire / PulseAudio** (detección de frecuencia nativa y altavoz virtual `Telefono-USB`).
  * **Windows:** **WASAPI Loopback** mediante `PyAudioWPatch` (toma el audio del sistema sin cables virtuales adicionales).
  * **macOS:** **CoreAudio** (compatible con altavoces y dispositivos virtuales como *BlackHole 2ch*).
* **Control de señal en tiempo real:**
  * Selector de **salida de audio** (monitores del sistema y altavoz virtual `Telefono-USB`) con descripción legible e identificador interno; nunca lista micrófonos.
  * Ajuste de **ganancia en dB** (`-12 dB` a `+12 dB`) con **limitador suave (`tanh`)** para prevenir distorsión.
  * Botones de **silencio rápido** (conserva el nivel anterior) y **reset a 0.0 dB**, con el estado de silencio persistido.
  * Medidor de nivel de señal (VU meter) e indicador de saturación (**CLIP**).
  * Ajuste de tamaño de búfer / paquete USB (`ms`) para equilibrar entre latencia mínima y estabilidad.
* **Perfiles de búfer unificados:** Baja latencia, Equilibrado y Alta estabilidad, con los mismos nombres en PC y Android y valores adaptados a cada plataforma.
* **Ajustes persistentes:** perfil, salida de audio, frecuencia, ganancia, silencio, limitador, dispositivo y tema se restauran al reiniciar.
* **Tema claro/oscuro y accesibilidad:** botón para alternar el tema en las tres apps de PC y nombres accesibles para lectores de pantalla.
* **Comprobación de actualizaciones:** consulta GitHub Releases al iniciar (y cada 6 h), con botón manual y enlace de descarga.
* **Bandeja del sistema y reconexión automática:**
  * Minimización a la bandeja del sistema (*System Tray*) con indicador visual de estado por color.
  * Reconexión silenciosa tras suspensión del equipo, bloqueo de pantalla o reconexión del cable USB.
  * Visor de registros (*logs*) en vivo para diagnóstico rápido.
* **Android:** audio focus, adaptación al cambio de salida (USB/Bluetooth) y WakeLock durante la reproducción.

> **Compatibilidad:** desde 0.19 el protocolo está versionado e incluye el tamaño de paquete. Actualiza la app de PC y el APK Android a la misma versión; las versiones anteriores no son compatibles entre sí.

---

## Requisitos previos

### 1. En el teléfono Android
* Tener activada la **Depuración por USB** en *Ajustes → Sistema → Opciones para desarrolladores*.
* Tener instalada y abierta la aplicación receptora compatible (`com.usbaudio`), escuchando en el puerto TCP **`5000`**.

### 2. En la computadora (según tu sistema operativo)

| Sistema Operativo | Requisitos del sistema |
| :--- | :--- |
| **Linux** | `adb` (`android-tools`), `pipewire-pulse` o `pulseaudio` (`pactl` / `parec`), `libportaudio2` |
| **Windows 10/11** | **Android SDK Platform-Tools** (`adb.exe` en el `PATH` o dentro de una carpeta `adb/` junto al `.exe`) y **Google USB Driver** |
| **macOS** | **Android SDK Platform-Tools** (`brew install android-platform-tools`), `portaudio` y **BlackHole 2ch** (`brew install blackhole-2ch`) para capturar audio interno del sistema |

---

## Descarga y uso rápido

1. Descarga el binario correspondiente a tu sistema operativo desde la sección **[Releases](https://github.com/Poiat/SpeakerPhone-USB/releases)**.
2. Conecta tu teléfono Android por cable USB y acepta la solicitud de huella de depuración USB en la pantalla del móvil.
3. Abre la app receptora en el teléfono y activa el modo de escucha.
4. Ejecuta **SpeakerPhone-USB** en tu computadora:
   * En **Windows**, si aparece el aviso de *SmartScreen*, haz clic en **Más información → Ejecutar de todas formas**.
   * En **Linux**, otorga permisos de ejecución antes de abrirlo:
     ```bash
     chmod +x SpeakerPhone-USB-Linux-x86_64
     ./SpeakerPhone-USB-Linux-x86_64
     ```
5. Selecciona la salida de audio deseada (por ejemplo el monitor de tu altavoz o `Telefono-USB` en Linux, `[Sistema / Loopback]` en Windows o `BlackHole 2ch` en macOS) y ajusta la ganancia o latencia según tus preferencias.

---

## Ejecución desde el código fuente

Clona el repositorio e instala las dependencias de Python (`Python 3.11+`):

```bash
git clone https://github.com/Poiat/SpeakerPhone-USB.git
cd SpeakerPhone-USB
pip install -r requirements.txt
```

Para ejecutar las pruebas y el lint:

```bash
pip install -r requirements-dev.txt
python scripts/check_version.py
python -m pytest -q
ruff check --select E9,F app_settings.py app_icon.py app_version.py dsp.py \
  protocol.py update_checker.py updater.py update_window_qt.py theme.py \
  app_theme_qt.py speakerphone_linux.py speakerphone_win.py \
  speakerphone_mac.py scripts tests
```

Las dependencias están **fijadas por versión** para builds reproducibles
(`requirements.txt`, `requirements-dev.txt`, `requirements-build.txt`).
Dependabot propone actualizaciones semanales, `pip-audit` audita el árbol
completo en CI y cada release publica un `sbom.json`. Consulta
[CONTRIBUTING.md](CONTRIBUTING.md) para el flujo de release y
[SECURITY.md](SECURITY.md) para reportar vulnerabilidades.

---

## Registros y configuración

| Plataforma | Logs | Configuración |
| :--- | :--- | :--- |
| **Linux** | `~/.local/state/SpeakerPhone-USB/logs/` | `~/.config/SpeakerPhone-USB/settings.json` |
| **Windows** | `%LOCALAPPDATA%\SpeakerPhone-USB\logs\` | `%APPDATA%\SpeakerPhone-USB\settings.json` |
| **macOS** | `~/Library/Logs/SpeakerPhone-USB/` | `~/Library/Application Support/SpeakerPhone-USB/settings.json` |

Los logs rotan cada 2 MB (5 copias). Se puede cambiar la carpeta con `SPEAKERPHONE_LOG_DIR` y la de configuración con `SPEAKERPHONE_CONFIG_DIR`.

---

## Actualizaciones

Las apps de PC consultan `https://api.github.com/repos/Poiat/SpeakerPhone-USB/releases/latest` al iniciar y cada 6 horas. Cuando hay una versión nueva, abren una ventana con barra de progreso, descargan el paquete de la plataforma, verifican su SHA-256 contra `SHA256SUMS.txt` y aplican la actualización reiniciando la app. La descarga se puede cancelar; si la app no está empaquetada o la carpeta no es escribible, se conserva el paquete y se ofrece la carpeta para instalarlo manualmente.

El botón **Buscar actualizaciones** permite repetir la consulta. La app Android incluye el mismo botón de consulta manual y abre la descarga del APK.

---

## Publicación y firma

Los tags numéricos (por ejemplo `0.19`) disparan el workflow de release. Para firmar el APK de release, configura estos secrets en GitHub:

- `ANDROID_KEYSTORE_BASE64` (keystore codificado en base64)
- `ANDROID_KEYSTORE_PASSWORD`
- `ANDROID_KEY_ALIAS`
- `ANDROID_KEY_PASSWORD`

Sin ellos, el CI publica un APK debug con un aviso. El release incluye `SHA256SUMS.txt` con los checksums de los artefactos.

---

## Solución de problemas

- **`bool_dealloc` / SIGABRT en Linux:** requiere PySide6 6.11.2; 6.12.0 tiene una regresión de refcount en `Signal.emit()`. El CI verifica la versión.
- **No aparece el dispositivo:** comprueba `adb devices`, acepta la huella de depuración USB y, si hay varios móviles, desconecta los que no uses (se prioriza el que está listo).
- **Sin audio del sistema en Linux:** revisa que PipeWire/PulseAudio esté activo y selecciona el `[Monitor]` de tu salida o el altavoz virtual `Telefono-USB`.
- **Sin audio del sistema en Windows:** elige `[Sistema / Loopback]`; no requiere cable virtual. Los endpoints 5.1/7.1 se capturan con su formato nativo y se mezclan a estéreo para el móvil. Si el loopback falla, la app no cambia al micrófono: el detalle queda en el registro.
- **Sin audio del sistema en macOS:** instala `BlackHole 2ch` y selecciónalo como salida de audio.
- **Cortes o latencia alta:** prueba el perfil **Alta estabilidad** y revisa el indicador de underruns en Android.
