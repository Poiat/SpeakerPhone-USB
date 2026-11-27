# Changelog

## 0.25.0

- Protocolo v2: Android envía telemetría cada ~500 ms por el mismo socket (underruns, búfer activo y backlog); PC y APK deben actualizarse juntos.
- PC: muestra la telemetría del móvil en la tarjeta de estado y sube automáticamente su búfer un paso (8 → 15 → 30 → 60 ms) cuando el móvil está cerca de su máximo y hay underruns persistentes. El ajuste es temporal y se restablece al reiniciar o cambiar el perfil.
- `check_version.py` valida también las constantes de protocolo compartidas entre Python y Kotlin.
- Tests del formato de telemetría, de la decisión de ajuste y del lector en Linux.
- Versión 0.25.0 en PC y Android.

## 0.24.0

- Interfaz: hoja de estilos compartida con tarjetas, badges, sliders y scrollbars para Linux, Windows y macOS; Linux y Windows usan el mismo nombre y estructura visual.
- Windows: paridad con Linux con mute (conserva la ganancia), reset a `0.0 dB`, búfer personalizado, copiar/limpiar registro, acciones de silencio en la bandeja y los mismos perfiles de búfer. Linux alinea su rango de ganancia a `±12 dB`.
- Resiliencia: perfil **Máxima estabilidad** (60 ms en PC, 72 ms en Android); búfer adaptativo en Android que crece ante underruns y se reduce tras 30 s estable; timeouts de envío con reconexión en Linux/Windows/macOS; prioridad alta del hilo de audio en Windows y buffers de socket ampliados.
- Tests para la hoja de estilos compartida.
- Versión 0.24.0 en PC y Android.

## 0.23.2

- Linux y Android: los bloques PCM se redondean a frames completos. Con 44.1 kHz y el perfil de baja latencia se generaban bloques de 1411 bytes y el audio llegaba desalineado (ruido e intercambio de canales).
- Android: el WakeLock y el audio focus se adquieren al iniciar cada sesión y se liberan al terminarla; ya no se retienen desde que arranca el servicio sin PC conectado.
- Actualizador: los scripts temporales de Windows y macOS usan nombres/carpetas aleatorias y exclusivas en lugar de una ruta predecible en el directorio temporal.
- Tests de regresión para la alineación PCM y para las rutas temporales del actualizador.
- Versión 0.23.2 en PC y Android.

## 0.23.1

- Windows: los dispositivos `[Loopback]` se abren con los canales y la frecuencia nativos de WASAPI; los endpoints 5.1/7.1 se mezclan a estéreo para el protocolo Android. Corrige `Invalid number of channels` al transmitir el audio del escritorio.
- Windows: si la salida elegida falla, ya no se cambia automáticamente al micrófono; se avisa en el registro y se reintenta la misma fuente.
- Windows: los fallos de apertura registran índice, nombre, hostapi, canales de entrada/salida, frecuencia nativa y parámetros exactos del stream.
- Linux: el selector solo muestra salidas de audio (monitores de PipeWire/PulseAudio y altavoz virtual `Telefono-USB`), con descripción legible e identificador interno.
- Linux: antes de abrir `parec` se valida que la fuente sea un monitor, evitando que caiga al micrófono predeterminado si la salida desaparece.
- Linux: botones de silencio rápido (conserva el nivel anterior) y reset a `0.0 dB`, con el estado persistido.
- Linux: el registro muestra el stderr real de `parec`, agrupa mensajes idénticos consecutivos con `(×N)` y añade botón Copiar.
- Versión 0.23.1 en PC y Android.

## 0.23.0

- La UI deja de repintar el medidor VU y el badge CLIP cuando la ventana no está visible, restaurada y en primer plano; al volver se refresca con el último estado.
- Windows/macOS: el estilo del badge CLIP solo se aplica cuando cambia el estado, no en cada lectura del medidor.
- Linux: la ventana se restaura correctamente desde la bandeja con `showNormal()`.
- Ventana de actualización con tema claro/oscuro y nombres accesibles.
- Versión 0.23.0 en PC y Android.

## 0.22.1

- Actualizador: elimina el paquete temporal tras aplicar la actualización; antes quedaban ~90 MB por actualización en `/tmp` (RAM en sistemas con tmpfs).
- Windows/macOS: el registro en pantalla se limita a 300 líneas para evitar crecimiento indefinido.
- Linux: el watcher de suspensión/bloqueo se reinicia correctamente y no duplica procesos `gdbus`.
- Linux (fallback Tk): corregido un `NameError` al mostrar errores de actualización.
- CI: ruff revisa también los entrypoints de Linux, Windows y macOS.
- Versión 0.22.1 en PC y Android.

## 0.22

- Tema claro/oscuro seleccionable en las tres apps de PC, persistente entre sesiones.
- Nombres accesibles (`AccessibleName`) en los controles principales de Linux, Windows y macOS para lectores de pantalla.
- Windows: errores de audio aislados con fallback y logs forzados a UTF-8.
- Dependencias fijadas por versión para builds reproducibles; `pip-audit` en CI y SBOM (`cyclonedx`) en CI y releases.
- Licencia MIT, guía de contribución, política de seguridad, Dependabot y plantillas de issues/PR.
- Badges de CI, release y licencia en el README.
- Versión 0.22 en PC y Android.

## 0.20

- Actualización en vivo en PC: ventana con barra de progreso, descarga automática, verificación SHA-256 y aplicación/relanzamiento en Linux, Windows y macOS.
- Botón de actualizaciones manual en Android.
- Versión 0.20 en PC y Android.

## 0.19

- Protocolo v1 con magic, versión y `chunk_ms`; Android ajusta la lectura al paquete del PC.
- DSP compartido y vectorizado con NumPy en las tres apps de PC.
- Ajustes persistentes (perfil, fuente, frecuencia, ganancia, limitador y dispositivo).
- Android: audio focus, reconstrucción de `AudioTrack` al cambiar de salida, WakeLock sin caducidad y botón de búsqueda de actualizaciones.
- Preferencia por dispositivos ADB listos y limpieza de sinks virtuales huérfanos en Linux.
- Corrección del filtro CoreAudio en macOS.
- Tests de protocolo, DSP, ajustes y actualizaciones; validación de versión, checksums SHA-256 y build Linux en CI.
- Firma opcional del APK de release mediante secrets de keystore.
- Actualización periódica cada 6 h y botón de búsqueda manual.

## 0.18

- Icono `foid.png` en ventanas, bandeja, AppImage y launcher Android; `.ico`/`.icns` para Windows/macOS.
- Botón “Buscar actualizaciones” en las tres apps de PC.
- Versión visible `v0.18`.

## 0.17

- Comprobación de actualizaciones al iniciar contra GitHub Releases.
- Versión visible en las apps de PC.
- Etiquetas numéricas en el CI.

## 0.16

- Perfiles de búfer unificados entre Android y PC (8/15/30 ms en PC, 24/36/48 ms en Android).

## 0.15

- Fix del crash `bool_dealloc` fijando PySide6 6.11.2.
- Log del binding Qt al arrancar.

## 0.14

- Logs persistentes en disco con rotación, hooks de excepciones y botón para abrir la carpeta.

## 0.13

- Verificación de ADB instalado/funcionando en Linux, Windows y macOS.

## 0.12

- Verificación de ADB en la interfaz Linux.

## 0.11

- Ajustes para el crash `bool_dealloc` con PySide6.
