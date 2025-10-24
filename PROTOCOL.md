# Yun Bridge v2 - Serial RPC Protocol

Este documento detalla el protocolo de comunicación serial Remote Procedure Call (RPC) utilizado entre el procesador Linux (corriendo el demonio en Python) y el microcontrolador ATmega (corriendo el sketch en C++) en el ecosistema Arduino Yun Bridge v2.

## Arquitectura del Protocolo

El protocolo es binario y está estructurado en varias capas para garantizar la fiabilidad y la integridad de los datos:

1.  **Capa de Aplicación (Comandos):** Define las operaciones a ejecutar (ej. `digitalWrite`, `analogRead`).
2.  **Capa de Trama (Framing):** Envuelve los comandos en una estructura de trama consistente con encabezado, payload y checksum.
3.  **Capa de Codificación (Encoding):** Codifica la trama binaria usando **Consistent Overhead Byte Stuffing (COBS)** para eliminar todos los bytes `0x00` del contenido, permitiendo que `0x00` se use como un marcador de fin de paquete inequívoco.
4.  **Capa de Transporte (Transport):** Envía el paquete codificado a través de la línea serial, terminando con un byte `0x00`.

El flujo de un paquete desde el emisor al receptor es:

`Comando -> Trama Raw -> Trama Codificada con COBS -> Envío Serial (Trama COBS + 0x00)`

## 1. Estructura de la Trama (Raw Frame)

Antes de la codificación COBS, cada trama (o "paquete") tiene la siguiente estructura:

| Campo | Longitud (Bytes) | Tipo de Dato | Endianness | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| **Header** | **5** | `struct` | Little | Metadatos del paquete. |
| **Payload** | `N` | `uint8_t[]` | N/A | Datos específicos del comando. |
| **CRC** | **2** | `uint16_t` | Little | Checksum para la integridad de los datos. |

### 1.1. Encabezado (Header) - 5 Bytes

El encabezado está compuesto por los siguientes campos:

| Campo | Longitud (Bytes) | Tipo de Dato | Endianness | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `version` | 1 | `uint8_t` | N/A | Versión del protocolo. Valor fijo: `0x02`. |
| `payload_length` | 2 | `uint16_t` | Little | Longitud en bytes del campo `Payload`. |
| `command_id` | 2 | `uint16_t` | Little | Identificador numérico del comando o estado. |

**Importante:** La estructura del encabezado está "empaquetada" (packed) para asegurar que su tamaño sea exactamente de 5 bytes, sin bytes de relleno añadidos por el compilador.

### 1.2. Payload - 0 a 256 Bytes

El contenido del payload es específico para cada `command_id`. Su longitud máxima es de **256 bytes**. La estructura detallada para cada comando se define en la sección "Tabla de Comandos".

### 1.3. CRC (Checksum) - 2 Bytes

Para garantizar la integridad de los datos, se calcula un checksum sobre el **encabezado y el payload combinados**.

-   **Algoritmo:** CRC-16-CCITT
-   **Polinomio:** `0x1021` (x¹⁶ + x¹² + x⁵ + 1)
-   **Valor Inicial:** `0xFFFF`
-   **Endianness:** El resultado de 16 bits se adjunta al final de la trama en formato little-endian.

## 2. Codificación COBS y Transporte

1.  La trama completa (`Header` + `Payload` + `CRC`) se codifica utilizando el algoritmo **COBS**. Esto garantiza que no haya bytes nulos (`0x00`) dentro del contenido del paquete.
2.  El paquete codificado se envía a través del puerto serial.
3.  Se envía un byte `0x00` final para marcar el final del paquete.

El receptor lee del puerto serial hasta que encuentra un byte `0x00`, decodifica el paquete recibido usando COBS, verifica el CRC y, si todo es correcto, procesa el comando.

## 3. Tabla de Comandos y Estados

Los `command_id` se dividen en dos categorías: Comandos (generalmente enviados de Linux al MCU) y Estados/Respuestas (generalmente del MCU a Linux).

### 3.1. Códigos de Estado y Flujo

| ID (Hex) | Nombre | Dirección | Payload | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `0x00` | `STATUS_OK` | MCU -> Linux | Vacío | Indica que el comando fue recibido y ejecutado con éxito. |
| `0x01` | `STATUS_ERROR` | MCU -> Linux | Vacío | Error genérico durante la ejecución del comando. |
| `0x02` | `STATUS_CMD_UNKNOWN` | MCU -> Linux | Vacío | El `command_id` recibido no es reconocido. |
| `0x07` | `STATUS_ACK` | MCU -> Linux | Vacío | Acuse de recibo genérico para comandos que no tienen una respuesta específica. |
| `0x08` | `CMD_XOFF` | MCU -> Linux | Vacío | Pausa la transmisión de datos desde Linux (usado para el buffer de la consola). |
| `0x09` | `CMD_XON` | MCU -> Linux | Vacío | Reanuda la transmisión de datos desde Linux. |

### 3.2. Comandos de I/O de Pines

| ID (Hex) | Nombre | Dirección | Payload | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `0x10` | `CMD_SET_PIN_MODE` | Linux -> MCU | `[pin (1B)][mode (1B)]` | Configura el modo de un pin. `mode`: 0=INPUT, 1=OUTPUT, 2=INPUT_PULLUP. |
| `0x11` | `CMD_DIGITAL_WRITE` | Linux -> MCU | `[pin (1B)][value (1B)]` | Escribe un valor digital (0 o 1) en un pin. |
| `0x12` | `CMD_ANALOG_WRITE` | Linux -> MCU | `[pin (1B)][value (1B)]` | Escribe un valor analógico (PWM, 0-255) en un pin. |
| `0x13` | `CMD_DIGITAL_READ` | Linux -> MCU | `[pin (1B)]` | Solicita la lectura de un pin digital. |
| `0x14` | `CMD_ANALOG_READ` | Linux -> MCU | `[pin (1B)]` | Solicita la lectura de un pin analógico. |
| `0x15` | `CMD_DIGITAL_READ_RESP`| MCU -> Linux | `[pin (1B)][value (2B)]` | Respuesta a `CMD_DIGITAL_READ` con el valor del pin. |
| `0x16` | `CMD_ANALOG_READ_RESP` | MCU -> Linux | `[pin (1B)][value (2B)]` | Respuesta a `CMD_ANALOG_READ` con el valor del pin. |

### 3.3. Comandos de Consola

| ID (Hex) | Nombre | Dirección | Payload | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `0x20` | `CMD_CONSOLE_WRITE` | Bidireccional | `[string (N Bytes)]` | Envía una cadena de texto a la consola del otro procesador. |

### 3.4. Comandos de Mailbox

| ID (Hex) | Nombre | Dirección | Payload | Descripción |
| :--- | :--- | :--- | :--- | :--- |
| `0x41` | `CMD_MAILBOX_PROCESSED`| MCU -> Linux | `[mensaje (N Bytes)]` | Envía un mensaje procesado por el MCU de vuelta a Linux. |

*Nota: Otros comandos de Mailbox, Datastore, File y Process son manejados por la librería `Bridge.cpp` pero no se usan activamente en el sketch de ejemplo `BridgeControl.ino`.*
