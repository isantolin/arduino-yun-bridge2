# Yun Bridge v2 - Binary RPC Protocol

## 1. Visión general

Este documento describe el protocolo binario utilizado entre el microcontrolador (MCU) y el procesador Linux (MPU) en el ecosistema Arduino Yun Bridge v2. El protocolo expuesto aquí refleja el comportamiento real del firmware y del demonio publicados en este repositorio.

La **fuente de verdad** del protocolo reside en `tools/protocol/spec.toml`. Al ejecutar `python3 tools/protocol/generate.py` se regeneran tanto el módulo Python (`openwrt-yun-bridge/yunbridge/rpc/protocol.py`) como el encabezado de Arduino (`openwrt-library-arduino/src/protocol/rpc_protocol.h`). Este documento se mantiene sincronizado con esa especificación y debe revisarse si se añade o modifica un comando en el archivo TOML.

La comunicación sigue un modelo de RPC: normalmente el MPU inicia las peticiones y el MCU responde, aunque existen comandos simétricos (por ejemplo, para consola y mailbox).

## 2. Transporte

Los frames se encapsulan con **Consistent Overhead Byte Stuffing (COBS)** y cada frame codificado termina en `0x00` (definido como `RPC_FRAME_DELIMITER`).

- `MAX_PAYLOAD_SIZE = 128` bytes. Todo payload que exceda ese tamaño es truncado por la implementación antes de enviarse.
- Todos los enteros multi-byte se codifican en **Big Endian**.

## 3. Formato de frame (antes de COBS)

+--------------------------+---------------------+----------+ | Cabecera (5 bytes) | Payload (0-128) | CRC32 | +--------------------------+---------------------+----------+


### 3.1 Cabecera

| Campo            | Tipo      | Descripción                                   |
| ---------------- | --------- | --------------------------------------------- |
| `version`        | `uint8_t` | Versión del protocolo. Valor actual: `0x02`.  |
| `payload_length` | `uint16_t`| Longitud del payload en bytes.                |
| `command_id`     | `uint16_t`| Identificador del comando o estado enviado.   |

### 3.2 CRC

El CRC (4 bytes, Big Endian) cubre cabecera + payload y utiliza CRC-32 IEEE 802.3 (polinomio reflejado `0x04C11DB7` / normal `0xEDB88320`, estado inicial `0xFFFFFFFF`, XOR final `0xFFFFFFFF`).

## 4. Códigos de estado (`Status`)

Los códigos de estado ocupan el rango `0x30` - `0x3F` para evitar colisiones con caracteres de control ASCII (XON/XOFF/CR/LF).

| Código | Nombre             | Uso típico                                         |
| ------ | ------------------ | -------------------------------------------------- |
| `0x30` | `STATUS_OK`        | Operación completada correctamente.               |
| `0x31` | `STATUS_ERROR`     | Fallo genérico. |
| `0x32` | `STATUS_CMD_UNKNOWN` | Comando no reconocido.                           |
| `0x33` | `STATUS_MALFORMED` | Payload con formato inválido.                     |
| `0x34` | `STATUS_OVERFLOW`  | El frame recibido excede el tamaño del buffer interno. |
| `0x35` | `STATUS_CRC_MISMATCH` | CRC inválido.                                   |
| `0x36` | `STATUS_TIMEOUT`   | Timeout al ejecutar la operación.                 |
| `0x37` | `STATUS_NOT_IMPLEMENTED` | Comando definido pero no soportado.         |
| `0x38` | `STATUS_ACK`       | Acknowledgement genérico para operaciones fire-and-forget. |

## 5. Comandos

### 5.1 Sistema y control de flujo (0x40 – 0x4F)

- **`0x40` CMD_GET_VERSION (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x41 CMD_GET_VERSION_RESP`): `[version_major: u8, version_minor: u8]`.

- **`0x42` CMD_GET_FREE_MEMORY (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x43 CMD_GET_FREE_MEMORY_RESP`): `[free_memory: u16]`.

- **`0x44` CMD_LINK_SYNC (Linux → MCU)**
  - Petición: `nonce: byte[16]`. Handshake de seguridad.
  - Respuesta (`0x45 CMD_LINK_SYNC_RESP`, MCU → Linux): `nonce || tag`. Donde `tag = HMAC-SHA256(secret, nonce)` (16 bytes).

- **`0x46` CMD_LINK_RESET (Linux → MCU)**
  - Petición: opcionalmente `[ack_timeout: u16, retry_limit: u8, response_timeout: u32]` para reconfigurar timeouts.
  - Respuesta (`0x47 CMD_LINK_RESET_RESP`): sin payload.

- **`0x48` CMD_GET_TX_DEBUG_SNAPSHOT (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x49 CMD_GET_TX_DEBUG_SNAPSHOT_RESP`): `[pending_count: u8, awaiting_ack: u8, retry_count: u8, last_cmd_id: u16, last_send_ms: u32]`. Diagnóstico del estado interno del transporte.

- **`0x4A` CMD_SET_BAUDRATE (Linux → MCU)**
  - Petición: `[baudrate: u32]`. Cambia la velocidad del puerto serie en caliente.
  - Respuesta (`0x4B CMD_SET_BAUDRATE_RESP`): sin payload (confirmación antes del cambio).

- **`0x4E` CMD_XOFF (MCU → Linux)** / **`0x4F` CMD_XON (MCU → Linux)**
  - Sin payload. Controlan el flujo de datos de la consola para evitar desbordamientos en el MCU.

### 5.2 GPIO (0x50 – 0x5F)

- **`0x50` CMD_SET_PIN_MODE (Linux → MCU)**: `[pin: u8, mode: u8]` (`mode`: 0=INPUT, 1=OUTPUT, 2=INPUT_PULLUP).
- **`0x51` CMD_DIGITAL_WRITE (Linux → MCU)**: `[pin: u8, value: u8]`.
- **`0x52` CMD_ANALOG_WRITE (Linux → MCU)**: `[pin: u8, value: u8]`.
- **`0x53` CMD_DIGITAL_READ (Linux → MCU)**: `[pin: u8]`. Respuesta `0x55 CMD_DIGITAL_READ_RESP`: `[value: u8]`.
- **`0x54` CMD_ANALOG_READ (Linux → MCU)**: `[pin: u8]`. Respuesta `0x56 CMD_ANALOG_READ_RESP`: `[value: u16]`.

### 5.3 Consola (0x60)

- **`0x60` CMD_CONSOLE_WRITE (bidireccional)**
  - Payload: `chunk: byte[]` (máx. 128 bytes). Datos crudos de la consola serie virtual.

### 5.4 Datastore (0x70)

- **`0x70` CMD_DATASTORE_PUT (MCU → Linux)**: `[key_len: u8, key: char[], value_len: u8, value: char[]]`.
- **`0x71` CMD_DATASTORE_GET (MCU → Linux)**: `[key_len: u8, key: char[]]`.
- **`0x72` CMD_DATASTORE_GET_RESP (Linux → MCU)**: `[value_len: u8, value: char[]]`.

### 5.5 Mailbox (0x80)

- **`0x80` CMD_MAILBOX_READ (MCU → Linux)**: sin payload. Respuesta `0x84 CMD_MAILBOX_READ_RESP` con `[message_len: u16, message: byte[]]`.
- **`0x81` CMD_MAILBOX_PROCESSED (MCU → Linux)**: `[message_id: u16]` (opcional).
- **`0x82` CMD_MAILBOX_AVAILABLE (MCU → Linux)**: sin payload. Respuesta `0x85 CMD_MAILBOX_AVAILABLE_RESP` con `[count: u8]`.
- **`0x83` CMD_MAILBOX_PUSH (MCU → Linux)**: `[message_len: u16, message: byte[]]`. Mensaje hacia Linux.

### 5.6 Sistema de archivos (0x90)

- **`0x90` CMD_FILE_WRITE (MCU → Linux)**: `[path_len: u8, path: char[], data_len: u16, data: byte[]]`.
- **`0x91` CMD_FILE_READ (MCU → Linux)**: `[path_len: u8, path: char[]]`. Respuesta `0x93 CMD_FILE_READ_RESP` con `[data_len: u16, data: byte[]]`.
- **`0x92` CMD_FILE_REMOVE (MCU → Linux)**: `[path_len: u8, path: char[]]`.

### 5.7 Gestión de procesos (0xA0)

- **`0xA0` CMD_PROCESS_RUN (MCU → Linux)**
  - Payload: `command: char[]` (UTF-8). Ejecución bloqueante.
  - Respuesta `0xA4 CMD_PROCESS_RUN_RESP`: `[status: u8, stdout_len: u16, stdout: byte[], stderr_len: u16, stderr: byte[]]`.

- **`0xA1` CMD_PROCESS_RUN_ASYNC (MCU → Linux)**
  - Payload: `command: char[]` (UTF-8). Ejecución no bloqueante.
  - Respuesta `0xA5 CMD_PROCESS_RUN_ASYNC_RESP`: `[process_id: u16]`.

- **`0xA2` CMD_PROCESS_POLL (MCU → Linux)**
  - Petición: `[process_id: u16]`.
  - Respuesta `0xA6 CMD_PROCESS_POLL_RESP`: `[status: u8, exit_code: u8, stdout_len: u16, stdout: byte[], stderr_len: u16, stderr: byte[]]`.

- **`0xA3` CMD_PROCESS_KILL (MCU → Linux)**
  - Payload: `[process_id: u16]`.

## 6. Consideraciones adicionales

- **Truncado de datos:** Si una respuesta supera `MAX_PAYLOAD_SIZE`, los datos se truncan.
- **Buffers de Procesos:** Linux mantiene los buffers de `stdout`/`stderr` hasta que son leídos completamente (longitud 0).
- **MQTT:** El demonio expone la mayoría de estas operaciones en tópicos MQTT (`br/gpio`, `br/process`, `br/datastore`, etc.) facilitando la integración externa.
