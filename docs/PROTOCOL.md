# Protocolo de audio PC ↔ Android

Transporte TCP sobre `adb forward tcp:5000 tcp:5000`.

## Handshake

1. Android escucha en `127.0.0.1:5000`.
2. Al aceptar, Android escribe un byte `R` (0x52).
3. El PC envía un header de **11 bytes**:

| Campo | Tipo | Bytes | Descripción |
| :--- | :--- | :--- | :--- |
| magic | `2s` | 2 | `UA` |
| version | `B` | 1 | `1` |
| rate | `I` | 4 | Frecuencia en Hz (big-endian) |
| channels | `B` | 1 | 1 o 2 |
| bits | `B` | 1 | 16 |
| chunk_ms | `H` | 2 | Duración del paquete en ms (2–100) |

Formato Python: `>2sBIBBH`.

4. Después del header, el PC envía PCM S16LE continuo en paquetes de `chunk_ms`.
   Android lee bloques del mismo tamaño (`frameBytes * rate * chunk_ms / 1000`).

## Compatibilidad

El protocolo se versiona en el byte `version`. Un cliente con versión distinta
debe rechazar la sesión y el usuario debe actualizar PC y Android a la misma
versión. Desde `0.19`, ambos lados comparten el mismo conjunto de perfiles de
búfer, pero el búfer de reproducción de Android sigue siendo independiente del
tamaño de paquete de PC.

## Errores

- Magic o versión inválidos: se cierra la sesión; el PC reintenta.
- `rate` fuera de 8000–192000, canales distintos de 1/2, bits distintos de 16
  o `chunk_ms` fuera de 2–100: sesión rechazada.
