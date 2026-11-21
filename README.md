# SpeakerPhone-USB

Transmite el audio de tu computadora hacia tu teléfono Android en tiempo real a través de un cable USB (mediante el túnel TCP de **ADB**), permitiendo usar los altavoces o audífonos conectados al móvil como salida de audio de baja latencia sin depender de Bluetooth ni Wi-Fi.

---

## Características principales

* **Multiplataforma:** Ejecutables portátiles independientes para **Linux (x86_64)**, **Windows (x86_64)** y **macOS (Apple Silicon arm64)**.
* **Captura de audio nativa por sistema:**
  * **Linux:** Integración directa con **PipeWire / PulseAudio** (detección de frecuencia nativa y creación de sink virtual `Telefono-USB`).
  * **Windows:** Captura **WASAPI Loopback** mediante `PyAudioWPatch` (graba el audio del sistema sin cables virtuales adicionales).
  * **macOS:** Captura mediante **CoreAudio** (compatible con micrófonos y dispositivos virtuales como *BlackHole 2ch*).
* **Control de señal en tiempo real:**
  * Selector de dispositivo/fuente de entrada con actualización dinámica.
  * Ajuste de **ganancia en dB** (`-12 dB` a `+12 dB`) con **limitador suave (`tanh`)** para prevenir distorsión.
  * Medidor de nivel de señal (VU meter) e indicador de saturación (**CLIP**).
  * Ajuste de tamaño de búfer / paquete USB (`ms`) para equilibrar entre latencia mínima y estabilidad.
* **Bandeja del sistema y reconexión automática:**
  * Minimización a la bandeja del sistema (*System Tray*) con indicador visual de estado por color.
  * Reconexión silenciosa tras suspensión del equipo, bloqueo de pantalla o reconexión del cable USB.
  * Visor de registros (*logs*) en vivo para diagnóstico rápido.

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
5. Selecciona la fuente de audio deseada (por ejemplo `[Sistema / Loopback]` en Windows o `BlackHole 2ch` en macOS) y ajusta la ganancia o latencia según tus preferencias.

---

## Ejecución desde el código fuente

Clona el repositorio e instala las dependencias de Python (`Python 3.11+`):

```bash
git clone [https://github.com/Poiat/SpeakerPhone-USB.git](https://github.com/Poiat/SpeakerPhone-USB.git)
cd SpeakerPhone-USB
pip install -r requirements.txt
