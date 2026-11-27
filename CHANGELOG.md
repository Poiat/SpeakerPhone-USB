# Changelog

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
