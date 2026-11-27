# Seguridad

## Versiones soportadas

Solo la última release recibe correcciones de seguridad.

## Reportar una vulnerabilidad

No abras un issue público para vulnerabilidades. Usa un
[aviso de seguridad privado de GitHub](https://github.com/Poiat/SpeakerPhone-USB/security/advisories/new)
e incluye:

- Versión y plataforma afectadas.
- Pasos para reproducir.
- Impacto esperado.
- Logs o capturas, sin datos personales ni credenciales.

Recibirás una respuesta lo antes posible. No publiques detalles hasta que se
haya coordinado una corrección.

## Superficie de riesgo conocida

- **Actualizador:** descarga y ejecuta binarios desde GitHub Releases. Verifica
  SHA-256 contra `SHA256SUMS.txt`, pero ambos vienen del mismo origen; una firma
  criptográfica del manifiesto está pendiente.
- **Túnel ADB/TCP:** el protocolo escucha en `127.0.0.1:5000` a través de
  `adb forward` y no autentica clientes. Cualquier proceso local con acceso al
  puerto podría enviar audio.
- **Firma del APK:** las builds sin secrets de keystore se firman en modo debug
  y se publican solo para pruebas.
- **Logs y ajustes:** pueden contener modelos/seriales de dispositivo y rutas
  locales. Revísalos antes de compartirlos.

## Buenas prácticas para usuarios

- Descarga binarios únicamente desde la página oficial de releases.
- Mantén la app actualizada y verifica el checksum publicado.
- No ejecutes el AppImage/EXE con privilegios de administrador.
