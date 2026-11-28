# Protocolo de audio PC ↔ Android

Transporte TCP sobre `adb forward tcp:5000 tcp:5000`.

## Handshake

1. Android escucha en `127.0.0.1:5000`.
2. Al aceptar, Android escribe un byte `R` (0x52).
3. El PC envía un header de **11 bytes**:

| Campo | Tipo | Bytes | Descripción |
| :--- | :--- | :--- | :--- |
| magic | `2s` | 2 | `UA` |
| version | `B` | 1 | `2` |
| rate | `I` | 4 | Frecuencia en Hz (big-endian) |
| channels | `B` | 1 | 1 o 2 |
| bits | `B` | 1 | 16 |
| chunk_ms | `H` | 2 | Duración del paquete en ms (2–100) |

Formato Python: `>2sBIBBH`.

4. Después del header, el PC envía PCM S16LE continuo en paquetes de `chunk_ms`.
   Android lee bloques del mismo tamaño (`frameBytes * rate * chunk_ms / 1000`).

## Canal de retorno (v2)

Android envía telemetría por el mismo socket, en sentido contrario al PCM,
aproximadamente cada 500 ms:

| Campo | Tipo | Bytes | Descripción |
| :--- | :--- | :--- | :--- |
| magic | `2s` | 2 | `US` |
| version | `B` | 1 | `2` |
| underruns | `I` | 4 | Underruns acumulados del `AudioTrack` |
| buffer_ms | `H` | 2 | Búfer activo (incluye el adaptativo) |
| backlog_ms | `H` | 2 | Audio pendiente en el socket |
| flags | `B` | 1 | Bit 0: sesión reproduciendo |

Formato Python: `>2sBIHHB` (12 bytes).

El PC lee estos frames en un hilo independiente mientras envía audio. Si el
búfer del móvil está cerca del máximo (≥ 96 ms), hay underruns nuevos durante
tres intervalos seguidos y pasaron al menos 30 s, el PC sube su búfer un paso
(8 → 15 → 30 → 60 ms) y reabre la sesión. El ajuste es temporal: se mantiene
hasta reiniciar la app o cambiar el perfil manualmente.

## Compatibilidad

El protocolo se versiona en el byte `version`. Un cliente con versión distinta
debe rechazar la sesión y el usuario debe actualizar PC y Android a la misma
versión. La versión 2 añade el canal de retorno; PC y APK deben actualizarse
juntos. Desde `0.19`, ambos lados comparten el mismo conjunto de perfiles de
búfer. Desde `0.25.1`, Android garantiza que su búfer de reproducción sea al
menos `chunk_ms + 16 ms`, de modo que un paquete del PC mayor que el perfil
elegido en el móvil no deje el `AudioTrack` sin datos.

## Errores

- Magic o versión inválidos: se cierra la sesión; el PC reintenta.
- `rate` fuera de 8000–192000, canales distintos de 1/2, bits distintos de 16
  o `chunk_ms` fuera de 2–100: sesión rechazada.
