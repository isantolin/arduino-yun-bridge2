# Yun Bridge v2 - Binary RPC Protocol

## 1. Visión general

Este documento describe el protocolo binario utilizado entre el microcontrolador (MCU) y el procesador Linux (MPU) en el ecosistema Arduino Yun Bridge v2. El protocolo expuesto aquí refleja el comportamiento real del firmware y del demonio publicados en este repositorio.

La comunicación sigue un modelo de RPC: normalmente el MPU inicia las peticiones y el MCU responde, aunque existen comandos simétricos (por ejemplo, para consola y mailbox).

## 2. Transporte

Los frames se encapsulan con **Consistent Overhead Byte Stuffing (COBS)** y cada frame codificado termina en `0x00`.

- `MAX_PAYLOAD_SIZE = 256` bytes. Todo payload que exceda ese tamaño es truncado por la implementación antes de enviarse.
- Todos los enteros multi-byte se codifican en **Big Endian**.

## 3. Formato de frame (antes de COBS)

```
+--------------------------+---------------------+----------+
|   Cabecera (5 bytes)     |   Payload (0-256)   |   CRC    |
+--------------------------+---------------------+----------+
```

### 3.1 Cabecera

| Campo            | Tipo      | Descripción                                   |
| ---------------- | --------- | --------------------------------------------- |
| `version`        | `uint8_t` | Versión del protocolo. Valor actual: `0x02`.  |
| `payload_length` | `uint16_t`| Longitud del payload en bytes.                |
| `command_id`     | `uint16_t`| Identificador del comando o estado enviado.   |

### 3.2 CRC

El CRC (2 bytes, Big Endian) cubre cabecera + payload y utiliza CRC-16-CCITT (polinomio `0x1021`, valor inicial `0xFFFF`).

## 4. Códigos de estado (`Status`)

| Código | Nombre             | Uso típico                                         |
| ------ | ------------------ | -------------------------------------------------- |
| `0x00` | `STATUS_OK`        | Operación completada correctamente.               |
| `0x01` | `STATUS_ERROR`     | Fallo genérico. El payload opcional `"process_poll_queue_full"` indica que el MCU alcanzó el máximo de 16 `CMD_PROCESS_POLL` pendientes. |
| `0x02` | `STATUS_CMD_UNKNOWN` | Comando no reconocido.                           |
| `0x03` | `STATUS_MALFORMED` | Payload con formato inválido.                     |
| `0x04` | `STATUS_CRC_MISMATCH` | CRC inválido.                                   |
| `0x05` | `STATUS_TIMEOUT`   | Timeout al ejecutar la operación.                 |
| `0x06` | `STATUS_NOT_IMPLEMENTED` | Comando definido pero no soportado.         |
| `0x07` | `STATUS_ACK`       | Acknowledgement genérico para operaciones fire-and-forget. |

> Nota: los `STATUS_*` pueden incluir un payload opcional en UTF-8 con una
> descripción breve del error (por ejemplo, `"not_found"`). El sketch del MCU
> debe registrar un callback de estado para reaccionar ante estos avisos.

## 5. Comandos

En cada comando se indica la dirección principal (`Linux → MCU`, `MCU → Linux` o bidireccional) y la estructura de payload real utilizada por la implementación.

### 5.1 Sistema y control de flujo (0x00 – 0x0F)

- **`0x00` GET_VERSION (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x80 GET_VERSION_RESP`): `[version_major: u8, version_minor: u8]`.
  - El daemon `bridge_daemon.py` envía automáticamente este comando tras establecer la conexión serie y publica la respuesta en el tópico MQTT `br/system/version/value` con el formato `MAJOR.MINOR`.

- **`0x01` GET_FREE_MEMORY (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x82 GET_FREE_MEMORY_RESP`): `[free_memory: u16]`.

- **`0x08` CMD_XOFF (MCU → Linux)** / **`0x09` CMD_XON (MCU → Linux)**
  - Sin payload. Controlan el flujo de datos de consola.

### 5.2 GPIO (0x10 – 0x1F)

- **`0x10` CMD_SET_PIN_MODE (Linux → MCU)**: `[pin: u8, mode: u8]` (`mode`: 0=INPUT, 1=OUTPUT, 2=INPUT_PULLUP).
- **`0x11` CMD_DIGITAL_WRITE (Linux → MCU)**: `[pin: u8, value: u8]`.
- **`0x12` CMD_ANALOG_WRITE (Linux → MCU)**: `[pin: u8, value: u8]`.
- **`0x13` CMD_DIGITAL_READ (Linux → MCU)**: `[pin: u8]`. Respuesta `0x15 CMD_DIGITAL_READ_RESP` (MCU → Linux): `[value: u8]`.
- **`0x14` CMD_ANALOG_READ (Linux → MCU)**: `[pin: u8]`. Respuesta `0x16 CMD_ANALOG_READ_RESP` (MCU → Linux): `[value: u16]`.

### 5.3 Consola (0x20)

- **`0x20` CMD_CONSOLE_WRITE (bidireccional)**
  - Payload: `chunk: byte[]` (máx. 256 bytes). No se envía prefijo de longitud; los datos se segmentan en bloques de hasta `MAX_PAYLOAD_SIZE`.

- **`0x30` CMD_DATASTORE_PUT (MCU → Linux)**: `[key_len: u8, key: char[], value_len: u8, value: char[]]`.
- **`0x31` CMD_DATASTORE_GET (Linux → MCU)**: `[key_len: u8, key: char[]]`.
- **`0x81` CMD_DATASTORE_GET_RESP (MCU → Linux)**: `[value_len: u8, value: char[]]`. El MCU actualiza la caché local del demonio con el valor recibido.

  - MQTT: las escrituras se publican en `br/datastore/put/<clave>` y el demonio replica el valor resultante en `br/datastore/get/<clave>`. Para solicitar un valor vía MQTT, los clientes publican un mensaje vacío en `br/datastore/get/<clave>/request`. El demonio responde siempre en `br/datastore/get/<clave>` (payload vacío si la clave no existe), evitando que el cliente consuma su propio mensaje.

### 5.5 Mailbox (0x40 – 0x4F)

- **`0x40` CMD_MAILBOX_READ (MCU → Linux)**: sin payload. Respuesta `0x90 CMD_MAILBOX_READ_RESP (Linux → MCU)` con `[message_len: u16, message: byte[]]` (truncado si supera 254 bytes).
- **`0x41` CMD_MAILBOX_PROCESSED (MCU → Linux)**: payload opcional.
  - `[message_id: u16]` si se dispone de identificador; puede enviarse vacío y el demonio lo interpreta como confirmación genérica.
- **`0x42` CMD_MAILBOX_AVAILABLE (MCU → Linux)**: sin payload. Respuesta `0x92 CMD_MAILBOX_AVAILABLE_RESP (Linux → MCU)` con `[count: u8]`.
  - La librería Arduino expone esta consulta mediante `Mailbox.requestAvailable()` y entrega el resultado en `Bridge.onMailboxAvailableResponse`.
- **`0x43` CMD_MAILBOX_PUSH (MCU → Linux)**: `[message_len: u16, message: byte[]]`. El MCU publica datos hacia Linux; el demonio responde con `STATUS_ACK` independiente (frame `0x07`).

#### MQTT relacionado con Mailbox

- El demonio publica cada mensaje que llega desde el MCU en `br/mailbox/incoming` y mantiene una cola persistente (`mailbox_incoming_queue`) para que los clientes MQTT puedan consultarla posteriormente.
- Los clientes externos pueden solicitar el siguiente mensaje disponible publicando en `br/mailbox/read`; el demonio atiende primero la cola proveniente del MCU y, si está vacía, entrega el siguiente mensaje pendiente destinado al MCU (útil para pruebas cuando el sketch no está ejecutándose).
- `br/mailbox/incoming_available` expone el número de mensajes pendientes en la cola orientada a los clientes MQTT.
- Los mensajes publicados por MQTT en `br/mailbox/write` se encolan para el MCU. Mientras existan mensajes pendientes, el demonio publica la profundidad de esa cola en `br/mailbox/outgoing_available` y el firmware puede recuperarlos mediante `CMD_MAILBOX_READ`.

### 5.6 Sistema de archivos (0x50 – 0x5F)

- **`0x50` CMD_FILE_WRITE (MCU → Linux)**: `[path_len: u8, path: char[], data_len: u16, data: byte[]]`. Tras escribir, Linux responde con `STATUS_ACK` o `STATUS_ERROR`.
- **`0x51` CMD_FILE_READ (MCU → Linux)**: `[path_len: u8, path: char[]]`. Respuesta `0xA1 CMD_FILE_READ_RESP (Linux → MCU)` con `[data_len: u16, data: byte[]]`, truncada a 254 bytes si es necesario. Si la lectura falla, Linux envía `STATUS_ERROR` con un mensaje corto.
- **`0x52` CMD_FILE_REMOVE (MCU → Linux)**: `[path_len: u8, path: char[]]`. El demonio contesta con `STATUS_ACK` o `STATUS_ERROR`.

### 5.7 Gestión de procesos (0x60 – 0x6F)

- **`0x60` CMD_PROCESS_RUN (MCU → Linux)**
  - Payload: `command: UTF-8 bytes` (sin prefijo de longitud). El demonio aplica la lista blanca y timeout configurados.
  - Respuesta `0xB0 CMD_PROCESS_RUN_RESP (Linux → MCU)`: `[status: u8, stdout_len: u16, stdout: byte[], stderr_len: u16, stderr: byte[]]`. Las longitudes son Big Endian y se limitan para que el total no supere 256 bytes.

- **`0x61` CMD_PROCESS_RUN_ASYNC (MCU → Linux)**
  - Payload: `command: UTF-8 bytes` (sin prefijo de longitud).
  - Respuesta `0xB1 CMD_PROCESS_RUN_ASYNC_RESP (Linux → MCU)`: `[process_id: u16]`. El valor `0xFFFF` indica fallo al lanzar el proceso.

- **`0x62` CMD_PROCESS_POLL (MCU → Linux)**
  - Petición: `[process_id: u16]`.
  - Respuesta `0xB2 CMD_PROCESS_POLL_RESP (Linux → MCU)`: `[status: u8, exit_code: u8, stdout_len: u16, stdout: byte[], stderr_len: u16, stderr: byte[]]`.
    - `exit_code = 0xFF` cuando el proceso sigue en ejecución.
    - El daemon mantiene buffers persistentes por PID, por lo que lecturas consecutivas entregan datos sin duplicados hasta que ambos buffers quedan vacíos. El `exit_code` real se conserva y se sigue enviando hasta que el MCU confirma la lectura total (ambos `len = 0`).
    - En paralelo, se publica un mensaje MQTT con flags `stdout_truncated`/`stderr_truncated` que indican si aún quedan bytes pendientes en los buffers internos.
    - La librería Arduino reenvía automáticamente `CMD_PROCESS_POLL` cuando un fragmento contiene datos (`stdout_len > 0` o `stderr_len > 0`), garantizando que el sketch reciba toda la salida sin intervención manual.
     - El firmware del MCU acepta hasta 16 consultas pendientes simultáneas; si la cola está llena responde con `STATUS_ERROR` y el payload ASCII `process_poll_queue_full`.

- **`0x63` CMD_PROCESS_KILL (MCU → Linux)**
  - Payload: `[process_id: u16]`.
  - Respuesta: `STATUS_ACK` (`0x07`).

## 6. Consideraciones adicionales

- Todos los comandos que generan `STATUS_ACK` lo hacen mediante un frame independiente (comando `0x07`).
- Cuando se superan los límites de `MAX_PAYLOAD_SIZE`, los datos se truncan de forma silenciosa; esto aplica especialmente a `CMD_FILE_READ_RESP` y `CMD_PROCESS_*_RESP`. Los clientes deben manejar esta posibilidad y, si es necesario, solicitar reintentos segmentados.
- Las cadenas se intercambian en UTF-8 y pueden contener bytes nulos solo si la semántica del comando lo permite (por ejemplo, binarios en mailbox o archivos).
- El demonio publica en MQTT cualquier respuesta asíncrona relevante (por ejemplo, resultado de `CMD_PROCESS_POLL_RESP`) para que los clientes externos reciban el mismo estado que el MCU sin requerir frames adicionales.
- El daemon persiste los buffers `stdout`/`stderr` de cada proceso hasta que se consumen por completo. Los clientes deben seguir invocando `CMD_PROCESS_POLL` hasta recibir ambos tamaños en cero, momento en el que el PID se recicla.