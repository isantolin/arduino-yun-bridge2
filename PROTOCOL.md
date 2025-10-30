'''# Yun Bridge v2 - Binary RPC Protocol

## 1. Visión General

Este documento describe el protocolo de comunicación binario utilizado por el ecosistema Arduino Yun Bridge v2. El protocolo está diseñado para ser robusto y eficiente en enlaces serie entre el microcontrolador (MCU) y el procesador Linux (MPU).

La comunicación se basa en un mecanismo de Llamada a Procedimiento Remoto (RPC) donde el MPU actúa como maestro y el MCU como esclavo.

## 2. Capa de Transporte

Todos los mensajes (frames) se encapsulan utilizando **Consistent Overhead Byte Stuffing (COBS)** para un enmarcado fiable sobre el flujo serie. Cada frame COBS-encoded se termina con un byte `0x00`.

## 3. Estructura del Frame (Antes de COBS)

Un frame, antes de ser codificado con COBS, tiene la siguiente estructura:

```
+--------------------------+---------------------+----------+
|   Cabecera (Header)      |   Payload (Datos)   |   CRC    |
| (5 bytes)                | (0-256 bytes)       | (2 bytes)|
+--------------------------+---------------------+----------+
```

- **Endianness:** Todos los campos multi-byte (`uint16_t`) se codifican en **Big Endian** (Network Byte Order).

### 3.1. Cabecera (Header)

La cabecera tiene una longitud fija de 5 bytes.

| Campo            | Tipo     | Longitud | Orden de Bytes | Descripción                                      |
| ---------------- | -------- | -------- | -------------- | ------------------------------------------------ |
| `version`        | `uint8_t`  | 1 byte   | N/A            | Versión del protocolo. Actualmente `0x02`.       |
| `payload_length` | `uint16_t` | 2 bytes  | Big Endian     | Longitud en bytes del campo Payload.             |
| `command_id`     | `uint16_t` | 2 bytes  | Big Endian     | Identificador numérico del comando a ejecutar.   |

### 3.2. Payload (Datos)

El payload contiene los datos específicos del comando. Su longitud y estructura varían para cada `command_id`. Si un comando no requiere datos, este campo puede tener una longitud de 0.

### 3.3. CRC (Cyclic Redundancy Check)

El CRC se calcula sobre la **Cabecera y el Payload combinados**. Proporciona una capa de verificación de integridad para detectar corrupción de datos.

- **Algoritmo:** CRC-16-CCITT
- **Polinomio:** `0x1021`
- **Valor Inicial:** `0xFFFF`
- **Longitud:** 2 bytes
- **Orden de Bytes:** Big Endian

## 4. Códigos de Estado (`STATUS_CODE`)

Estos códigos son enviados por el MCU al MPU para indicar el resultado de una operación.

- **`0x00`: STATUS_OK**
- **`0x01`: STATUS_ERROR**
- **`0x02`: STATUS_CMD_UNKNOWN**
- **`0x03`: STATUS_MALFORMED**
- **`0x04`: STATUS_CRC_MISMATCH**
- **`0x05`: STATUS_TIMEOUT**
- **`0x06`: STATUS_NOT_IMPLEMENTED**
- **`0x07`: STATUS_ACK**

## 5. Lista de Comandos (`command_id`)

A continuación se listan los comandos definidos, junto con la estructura de su payload.

### Comandos de Sistema y Control de Flujo (0x00 - 0x0F)

- **`0x00`: GET_VERSION**
  - **Descripción:** Solicita la versión del firmware del MCU.
  - **Payload de Petición:** (Vacío)
  - **Payload de Respuesta:** `[version_major: u8, version_minor: u8]`

- **`0x01`: GET_FREE_MEMORY**
  - **Descripción:** Solicita la cantidad de RAM libre en el MCU.
  - **Payload de Petición:** (Vacío)
  - **Payload de Respuesta:** `[free_memory: u16]` (Big Endian)

- **`0x08`: CMD_XOFF**
  - **Descripción:** (MCU -> Linux) Pausa la transmisión de datos.
  - **Payload de Petición:** (N/A)
  - **Payload de Respuesta:** (N/A)

- **`0x09`: CMD_XON**
  - **Descripción:** (MCU -> Linux) Reanuda la transmisión de datos.
  - **Payload de Petición:** (N/A)
  - **Payload de Respuesta:** (N/A)

### Comandos GPIO (0x10 - 0x1F)

*Nota sobre IDs de Respuesta:* Por convención, muchos comandos de respuesta tienen un ID que es el ID del comando original más un offset (ej. `0x80` para `GET_VERSION_RESP`). Sin embargo, `CMD_DIGITAL_READ_RESP` (0x15) y `CMD_ANALOG_READ_RESP` (0x16) son excepciones a esta convención, utilizando IDs secuenciales dentro de su bloque de comandos GPIO.

- **`0x10`: CMD_SET_PIN_MODE**
  - **Descripción:** Configura el modo de un pin GPIO.
  - **Payload:** `[pin: u8, mode: u8]` (mode: 0=INPUT, 1=OUTPUT, 2=INPUT_PULLUP)

- **`0x11`: CMD_DIGITAL_WRITE**
  - **Descripción:** Escribe un valor digital en un pin.
  - **Payload:** `[pin: u8, value: u8]`

- **`0x12`: CMD_ANALOG_WRITE**
  - **Descripción:** Escribe un valor analógico (PWM) en un pin.
  - **Payload:** `[pin: u8, value: u8]`

- **`0x13`: CMD_DIGITAL_READ**
  - **Descripción:** Lee el valor digital de un pin.
  - **Payload de Petición:** `[pin: u8]`
  - **Payload de Respuesta:** (Ver `CMD_DIGITAL_READ_RESP`)

- **`0x14`: CMD_ANALOG_READ**
  - **Descripción:** Lee el valor analógico de un pin.
  - **Payload de Petición:** `[pin: u8]`
  - **Payload de Respuesta:** (Ver `CMD_ANALOG_READ_RESP`)

- **`0x15`: CMD_DIGITAL_READ_RESP**
  - **Descripción:** Respuesta a `CMD_DIGITAL_READ`.
  - **Payload:** `[value: u8]` (0 o 1)

- **`0x16`: CMD_ANALOG_READ_RESP**
  - **Descripción:** Respuesta a `CMD_ANALOG_READ`.
  - **Payload:** `[value: u16]` (Big Endian)

### Comandos de Consola (0x20 - 0x2F)

- **`0x20`: CMD_CONSOLE_WRITE**
  - **Descripción:** Escribe datos en la consola del MCU.
  - **Payload:** `[message_len: u16, message: char[]]`

### Comandos de Datastore (0x30 - 0x3F)

- **`0x30`: CMD_DATASTORE_PUT**
  - **Descripción:** Almacena un par clave-valor en el MCU.
  - **Payload:** `[key_len: u8, key: char[], value_len: u8, value: char[]]`

- **`0x31`: CMD_DATASTORE_GET**
  - **Descripción:** Recupera un valor por su clave.
  - **Payload de Petición:** `[key_len: u8, key: char[]]`
  - **Payload de Respuesta:** (Ver `CMD_DATASTORE_GET_RESP`)

- **`0x81`: CMD_DATASTORE_GET_RESP**
  - **Descripción:** Respuesta a `CMD_DATASTORE_GET`.
  - **Payload:** `[value_len: u8, value: char[]]`

### Comandos de Mailbox (0x40 - 0x4F)

- **`0x40`: CMD_MAILBOX_READ**
  - **Descripción:** Lee un mensaje del buzón del MCU.
  - **Payload de Petición:** (Vacío)
  - **Payload de Respuesta:** (Ver `CMD_MAILBOX_READ_RESP`)

- **`0x41`: CMD_MAILBOX_PROCESSED**
  - **Descripción:** (MCU -> Linux) Indica que un mensaje del buzón ha sido procesado.
  - **Payload:** `[message_id: u16]` (Placeholder, not fully defined)

- **`0x42`: CMD_MAILBOX_AVAILABLE**
  - **Descripción:** (Linux -> MCU) Consulta si hay mensajes disponibles en el buzón.
  - **Payload de Petición:** (Vacío)
  - **Payload de Respuesta:** (Ver `CMD_MAILBOX_AVAILABLE_RESP`)

- **`0x90`: CMD_MAILBOX_READ_RESP**
  - **Descripción:** Respuesta a `CMD_MAILBOX_READ`.
  - **Payload:** `[message_len: u16, message: char[]]`

- **`0x92`: CMD_MAILBOX_AVAILABLE_RESP**
  - **Descripción:** Respuesta a `CMD_MAILBOX_AVAILABLE`.
  - **Payload:** `[count: u8]` (Número de mensajes disponibles)

### Comandos de Archivos (0x50 - 0x5F)

- **`0x50`: CMD_FILE_WRITE**
  - **Descripción:** Escribe contenido en un archivo en el sistema de archivos del MCU.
  - **Payload:** `[path_len: u8, path: char[], data_len: u16, data: byte[]]`

- **`0x51`: CMD_FILE_READ**
  - **Descripción:** Lee contenido de un archivo en el sistema de archivos del MCU.
  - **Payload de Petición:** `[path_len: u8, path: char[]]`
  - **Payload de Respuesta:** (Ver `CMD_FILE_READ_RESP`)

- **`0x52`: CMD_FILE_REMOVE**
  - **Descripción:** Elimina un archivo del sistema de archivos del MCU.
  - **Payload:** `[path_len: u8, path: char[]]`

- **`0xA1`: CMD_FILE_READ_RESP**
  - **Descripción:** Respuesta a `CMD_FILE_READ`.
  - **Payload:** `[data_len: u16, data: byte[]]`

### Comandos de Gestión de Procesos (0x60 - 0x6F)

- **`0x60`: CMD_PROCESS_RUN**
  - **Descripción:** Ejecuta un comando en el MPU y espera su finalización.
  - **Payload:** `[command_len: u16, command: char[]]`

- **`0x61`: CMD_PROCESS_RUN_ASYNC**
  - **Descripción:** Ejecuta un comando en el MPU de forma asíncrona (no espera finalización).
  - **Payload:** `[command_len: u16, command: char[]]`

- **`0x62`: CMD_PROCESS_POLL**
  - **Descripción:** Consulta el estado de un proceso asíncrono.
  - **Payload de Petición:** `[process_id: u16]`
  - **Payload de Respuesta:** (Ver `CMD_PROCESS_POLL_RESP`)

- **`0x63`: CMD_PROCESS_KILL**
  - **Descripción:** Termina un proceso en ejecución en el MPU.
  - **Payload:** `[process_id: u16]`

- **`0xB0`: CMD_PROCESS_RUN_RESP**
  - **Descripción:** Respuesta a `CMD_PROCESS_RUN`.
  - **Payload:** `[status: u8, stdout_len: u16, stdout: byte[], stderr_len: u16, stderr: byte[]]`

- **`0xB1`: CMD_PROCESS_RUN_ASYNC_RESP**
  - **Descripción:** Respuesta a `CMD_PROCESS_RUN_ASYNC`.
  - **Payload:** `[process_id: u16]`

- **`0xB2`: CMD_PROCESS_POLL_RESP**
  - **Descripción:** Respuesta a `CMD_PROCESS_POLL`.
  - **Payload:** `[status: u8, exit_code: u8, stdout_len: u16, stdout: byte[], stderr_len: u16, stderr: byte[]]`

'''