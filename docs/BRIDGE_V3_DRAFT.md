# Diseño Teórico: Arquitectura Bridge v3

Este documento recoge las propuestas arquitectónicas para una hipotética versión 3 del ecosistema Arduino MCU Bridge, enfocadas en maximizar el rendimiento (throughput), minimizar el footprint (RAM/CPU) y garantizar extensibilidad futura.

## 1. Capa de Enlace (Framing & Integridad)
* **COBS "In-Place" (Zero-Copy):** Decodificación directa sobre el buffer de recepción al detectar el delimitador. Ahorra 50% de RAM en el buffer serial.
* **Hash Criptográfico Liviano:** Transición de CRC-32 a Fletcher-16 o CRC-16 CCITT para tramas < 256 bytes. Ahorra 2 bytes por trama y 50% de ciclos de CPU sin perder confiabilidad matemática (misma distancia de Hamming para bloques pequeños).

## 2. Capa de Red y Transporte (Cabecera Ultra-Compacta y QoS)
* **Cabecera Bit-Packed Dinámica (1-3 bytes):** 
  * Bit 7: Tipo (0=Notificación, 1=Requiere ACK)
  * Bit 6: Compresión
  * Bits 4-5: Canal Lógico / Endpoint
  * Bits 0-3: Sequence Number (0-15)
* **Ventana Deslizante (Sliding Window):** El MCU envía hasta 15 mensajes sin esperar. El MPU responde con ACKs acumulativos, multiplicando el throughput (hasta 400%) al mitigar la latencia RTT del modelo Stop-and-Wait de V2.

## 3. Capa de Presentación (Empaquetado Zero-Copy Real)
* **Memory-Aligned Struct Overlays:** Diseñar payloads en `spec.toml` con padding implícito forzando offsets múltiplos de 4 para tipos grandes (`uint32_t`). 
* Permite decodificación O(1) vía `reinterpret_cast` directo sobre el buffer UART, eliminando copias en RAM (Zero-Copy) y previniendo Alignment Faults en arquitecturas de 32-bits (ESP32/ARM).

## 4. Compresión Delta y Tokenización (Mejor que RLE)
* **Compresión de Diccionario Estático Compartido:** Envío de Tokens pre-calculados (`0x01`) en lugar de strings repetitivos (ej. rutas de archivos), garantizado por el conocimiento simétrico de `spec.toml`.
* **Delta-Encoding para Sensores:** En telemetría periódica (ej. floats de 4 bytes), transmitir únicamente la diferencia (`int8_t`) respecto al último valor. Reduce tramas de actualización a 1 byte.

## 5. Multiplexación de Canales Virtuales (Extensibilidad)
* En lugar de bloquear la línea física con transferencias largas (ej. archivos bulk de 5MB), implementar "Streams" entrelazados (inspirado en HTTP/2).
* Permite que tramas de alta prioridad (Watchdog, Telemetría crítica) interrumpan tramas masivas de baja prioridad sin corromper el estado del buffer lógico.

## 6. Modelo Híbrido Adaptativo de Integridad
* **Comandos Efímeros (Telemetría pura):** Se prescinde del costoso CRC estricto. Se usa un XOR checksum de 8-bits. Si falla, la trama se descarta; la siguiente sobrescribe el estado en milisegundos.
* **Comandos Mutadores (Set Pin, Flash, Sync):** Exigen CRC-16 estricto o un Tag AEAD (Authenticated Encryption with Associated Data).
* **Autenticación AEAD en Vuelo:** El cálculo criptográfico (ej. Poly1305, ChaCha20) actúa simultáneamente como MAC y como Checksum electromagnético, proveyendo integridad física y defensa contra hardware-in-the-middle post-handshake.

## Conclusión Estratégica (V2 vs V3)
* **V2 Actual:** `[Header: 5 bytes] + [Payload: N] + [CRC-32: 4 bytes]` (Min. 9 bytes estáticos. Stop-and-Wait).
* **V3 Propuesta:** `[Header+FlowControl: 1 byte] + [Payload: N] + [Check: 1, 2 o 4 bytes]` (Min. 2 bytes para ACKs. Sliding Window. Multiplexación O(1)).


## Anexo A: Diseño a Nivel de Bits de la Cabecera V3 (Header Bit-Packing)

El objetivo de la V3 es abandonar los 5 bytes estáticos de la V2 (`version` + `payload_len` + `command_id`) a favor de una estructura dinámica ultra-compacta que parte de un único byte base.

### 1. El Byte de Control Primario (Frame Control Byte)
Toda trama V3 arranca siempre con **1 Byte de Control**, independientemente de lo que transporte. Este byte define la forma de procesar el resto de la trama.

**Estructura (8 bits):** `[ T | C | E E | S S S S ]`

*   **`T` (1 bit) - Frame Type:** 
    *   `0` = Datagrama/Notificación (No requiere ACK, tolerante a pérdida). Ej: Telemetría de un sensor.
    *   `1` = Reliable/Crítico (Requiere ACK, almacenado en ventana deslizante). Ej: Cambio de estado de un relé, handshake.
*   **`C` (1 bit) - Compression Flag:** 
    *   `1` = Payload comprimido (RLE o Tokenizado).
*   **`EE` (2 bits) - Endpoint / Logical Channel:** 
    *   Permite multiplexar 4 canales virtuales concurrentes para priorización O(1).
    *   `00` = `SYS` (Handshake, Watchdog, Ping, ACKs). *Máxima prioridad, no bloqueable.*
    *   `01` = `CTRL` (GPIO, Command_ID). *Prioridad alta.*
    *   `10` = `DATA` (Mailbox, Datastore). *Prioridad media.*
    *   `11` = `BULK` (File System I/O). *Prioridad baja, puede fragmentarse y ceder el paso a `SYS`.*
*   **`SSSS` (4 bits) - Sequence Number:**
    *   Contador módulo 16 (`0` a `15`).
    *   Vital para la Ventana Deslizante (Sliding Window). Permite enviar hasta 15 mensajes al hilo sin esperar un ACK. Si `T=1`, el receptor usará este número para enviar un "ACK Acumulativo".

### 2. El Campo de Longitud Variable (VarInt Payload Length)
En lugar de forzar siempre 2 bytes para la longitud (`uint16_t` de V2) cuando el 90% de las tramas ocupan menos de 64 bytes, V3 usará un entero de longitud variable (VarInt).

*   **Longitud < 128 bytes (El 90% de los casos):** Ocupa **1 byte**. (El bit más significativo está en `0`).
*   **Longitud >= 128 bytes:** Ocupa **2 o 3 bytes** (estilo LEB128 usado en Protobuf/WebAssembly).

### 3. El Identificador de Comando (Comando Dinámico)
*   Si el Endpoint es `SYS` (00) y la trama es un simple ACK (Payload Length = 0), no hay byte de comando. El Sequence Number basta.
*   Si hay Payload, el primer byte (o bytes) del Payload determina el identificador de la acción, el cual ahora está *scoped* (delimitado) por el Endpoint. No necesitas un `uint16_t` global; 256 comandos por Endpoint (`uint8_t`) son suficientes.

### 4. Resumen Visual de la Trama V3

**A) Trama de Telemetría (Ping, GPIO Read) -> Tamaño Total: 4 bytes**
*   Byte 0: `00010011` (Type:0, No-Comp:0, EndP:CTRL(01), Seq:3)
*   Byte 1: `0x01` (Payload Length: 1 byte)
*   Byte 2: `0x54` (Comando: AnalogRead Pin 4)
*   Byte 3: `0xXX` (Fletcher-16 / XOR checksum ligero, 1 byte)

**B) Trama Crítica (Handshake / File Write) -> Tamaño Total: N bytes**
*   Byte 0: `10110010` (Type:1 [ACK req], No-Comp:0, EndP:BULK(11), Seq:2)
*   Byte 1: `0x80 0x02` (Payload Length: VarInt para 256 bytes)
*   Bytes 2-257: `[...]` (Payload)
*   Bytes 258-259: `0xXXXX` (CRC-16 estricto, 2 bytes)


## Anexo B: Comparativa Cuantitativa V2 vs V3

Para justificar la complejidad de implementar la V3, aquí se presenta una comparativa analítica del impacto real sobre el bus UART.

### 1. Overhead por Trama

En la V2, toda trama paga un "impuesto" fijo por la cabecera y el CRC-32. En la V3, el impuesto es proporcional al tipo de mensaje.

| Escenario | Tamaño Payload | Overhead V2 (Fijo) | Overhead V3 (Dinámico) | Mejora V3 |
| :--- | :---: | :---: | :---: | :---: |
| **ACK Simple** | 0 bytes | 9 bytes (Header 5 + CRC 4) | **2 bytes** (Ctrl 1 + Chk 1) | **-77% (Ahorro de 7 bytes)** |
| **Digital Read (Ping)** | 1 byte | 9 bytes | **3 bytes** (Ctrl 1 + Len 1 + Chk 1) | **-66% (Ahorro de 6 bytes)** |
| **Escritura Archivo** | 64 bytes | 9 bytes | **4 bytes** (Ctrl 1 + Len 1 + CRC 2) | **-55% (Ahorro de 5 bytes)** |
| **Escritura Archivo Grande** | 256 bytes | 9 bytes | **5 bytes** (Ctrl 1 + Len 2 + CRC 2) | **-44% (Ahorro de 4 bytes)** |

*Nota: Todos asumen 1 byte adicional para el delimitador COBS `0x00` en ambas versiones (no listado).*

### 2. Rendimiento (Throughput) a 115200 Baudios

A 115200 baudios (sin paridad, 1 bit stop), transmitimos aprox. **11.520 bytes por segundo (11.5 KB/s)**.
El principal enemigo de V2 es la latencia (RTT) debido al modelo Stop-and-Wait.

**Escenario: Leer 100 sensores (1 byte c/u) de forma continua**

*   **V2 (Stop-and-Wait):**
    *   Trama Petición: 9 bytes
    *   Trama Respuesta: 10 bytes (9 overhead + 1 payload)
    *   Total por ciclo: 19 bytes.
    *   Costo de 100 lecturas: 1.900 bytes.
    *   Tiempo teórico de I/O: 164 ms.
    *   **Tiempo real (RTT):** Asumiendo 1ms de latencia por el switch de contexto USB/Linux por mensaje = 164 ms + 100 ms (RTT) = **264 ms**.
*   **V3 (Sliding Window):**
    *   Linux envía 15 peticiones seguidas (Pipeline de 15 tramas x 3 bytes) = 45 bytes.
    *   MCU responde con 15 lecturas seguidas (15 tramas x 4 bytes) = 60 bytes.
    *   Total por ciclo (15 lecturas): 105 bytes.
    *   Costo de 100 lecturas: ~700 bytes.
    *   Tiempo teórico de I/O: 60 ms.
    *   **Tiempo real (RTT):** Solo hay 6 RTTs (bloques de 15) en lugar de 100 = 60 ms + 6 ms = **66 ms**.

**Conclusión de Throughput:** V3 es un **400% más rápido** en operaciones intensivas de I/O gracias a la erradicación del Stop-and-Wait (Sliding Window) y la reducción del overhead.

### 3. Consumo de Memoria (RAM del MCU)

| Métrica | V2 (Actual) | V3 (PoC Propuesto) | Beneficio V3 |
| :--- | :--- | :--- | :--- |
| **RX Buffer** | 512 B | 512 B | Sin cambios |
| **COBS Decode Buf** | 512 B | **0 B** | **-512 B (Zero-Copy in-place)** |
| **Struct Parsed** | ~64 B (Copia) | **0 B** (Puntero `reinterpret_cast`) | **-64 B en Stack** |
| **TX Queue** | 2 a 4 mensajes (Estático) | 16 mensajes (1 byte/estado) | Menos memoria, más slots. |
| **CPU Time CRC** | ~1.5 µs por byte (CRC32 tabla) | **~0.2 µs por byte** (XOR 8-bit) | CPU libre para la aplicación de usuario |

### Conclusión Final

La versión 3 no es un mero "ajuste" estético del protocolo, es una reingeniería profunda para llevar el hardware al extremo termodinámico. Convierte el bus Serial en una red de alta frecuencia capaz de competir con I2C en latencia y SPI en throughput efectivo, liberando entre 500 y 1000 bytes de RAM en microcontroladores AVR donde cada byte es crítico.
