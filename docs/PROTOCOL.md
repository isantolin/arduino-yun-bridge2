# MCU Bridge v2 — Protocol & Architecture

Este documento unifica y reemplaza documentación histórica y dispersa.

## Mini-diagrama (flujos y direccionalidad)

```
                 ┌──────────────────────────────────────────────┐
                 │                MQTT v5 Broker                │
                 └──────────────────────────────────────────────┘
                               ▲                     │
                               │ daemon publishes     │ clients publish
                               │ (responses/snapshots)│ (commands)
                               │                     ▼
                      ┌────────────────────────────────────┐
                      │   McuBridge daemon (Linux / MPU)   │
                      │  - Policy (allow/deny)             │
                      │  - Dispatcher (MCU + MQTT routes)  │
                      │  - RuntimeState snapshots          │
                      └────────────────────────────────────┘
                               ▲
                               │ Serial RPC frames (COBS + CRC)
                               │
                               ▼
                      ┌────────────────────────────────────┐
                      │        MCU firmware (Arduino)      │
                      │  - Handshake HMAC-auth             │
                      │  - RPC command handlers            │
                      └────────────────────────────────────┘

Notas:
- Serial RPC: típicamente Linux→MCU requests y MCU→Linux responses, con comandos bidireccionales/push simétrico donde aplica.
- MQTT: clientes→daemon (comandos) y daemon→MQTT (respuestas/snapshots/metrics).
```

## Fuente de verdad

La **fuente de verdad machine-readable** del protocolo vive en `tools/protocol/spec.toml`. El sistema exige el cumplimiento estricto de la **versión 0x02**. Cualquier frame con una versión diferente es rechazado inmediatamente.

### Serialización de Payloads (Protobuf Enveloping)
El protocolo utiliza **Protobuf Enveloping** (Arquitectura v3) para unificar el transporte y el servicio. Todo el frame serial (header y payload) se encapsula en un mensaje Protobuf raíz (`RpcEnvelope`).

- **Python (daemon):** usa clases `Packet` generadas en `structures.py` sobre `google.protobuf`.
- **C++ (MCU):** usa nanopb para codificar/decodificar payloads sin heap dinámico en runtime. Los tipos se exponen como `rpc::payload::*` structs nativos con métodos `encode()`/`decode()`.

### Validación de Datos en el Borde (Bilateral Enforcement)
A partir de la v2.8.5, el sistema implementa **Validación Automática de Rangos**. Las restricciones de negocio (ej. `pin < 20`, `value <= 1024`) se definen en `spec.toml` y el generador produce código que:
- **MCU:** El método `decode()` de Nanopb rechaza tramas inválidas antes de que lleguen a la lógica de aplicación.
- **Daemon:** Utiliza `msgspec.Annotated` con metadatos de rango para validar entradas MQTT instantáneamente.

### Despacho de Comandos (O(1) Jump Tables)
El MCU utiliza un despacho basado en **tablas de salto (jump tables)** de punteros a métodos. Esto garantiza un tiempo de despacho constante (O(1)), eliminando la redundancia de código y cumpliendo con los requisitos de SIL-2.

## Transporte

Los frames se encapsulan con **Consistent Overhead Byte Stuffing (COBS)** y cada frame codificado termina en `0x00`.

- `MAX_PAYLOAD_SIZE = 64` bytes.
- Todos los enteros multi-byte se codifican en **Big Endian**.

## 3. Formato de frame (RpcEnvelope)

El frame es un mensaje Protobuf serializado seguido de un CRC32.

```
+---------------------------+-----------+
| Protobuf RpcEnvelope      | CRC32 (4) |
+---------------------------+-----------+
```

### 3.1 Campos del Envelope

| Campo | Tipo | Descripción |
| --- | --- | --- |
| `version` | `uint8` | Versión del protocolo (actual: `0x02`). |
| `command_id` | `uint16` | Identificador del comando o status. |
| `sequence_id` | `uint16` | Identificador de secuencia para deduplicación. |
| `nonce` | `byte[12]` | Nonce para cifrado AEAD (si aplica). |
| `tag` | `byte[16]` | Tag de autenticación Poly1305 (si aplica). |
| `payload` | `bytes` | Payload del mensaje (cifrado o plano). |

### 3.1.1 Seguridad AEAD (ChaCha20-Poly1305)

Si el enlace está sincronizado y el comando no es de sistema/estado:
- **Nonce (12 bytes):** Prefijo "MCU" o "DMN" + 8 bytes de contador monótono.
- **Payload:** Datos cifrados.
- **Tag (16 bytes):** Firma de autenticidad Poly1305 sobre el Header (Associated Data) + Payload.

### 3.2 CRC

CRC32 (4 bytes, Big Endian) sobre el RpcEnvelope serializado. Polinomio IEEE 802.3.

---

### 5.1.1 Capabilities Bitmask (u32)

| Bit | Valor | Feature | Descripción |
| :--- | :--- | :--- | :--- |
| 0 | `1` | Watchdog | MCU Watchdog habilitado. |
| 1 | `2` | (Reservado) | Anteriormente RLE. |
| 2 | `4` | Debug Frames | Logging de tramas activo. |
| 3 | `8` | Debug IO | Logging de GPIO activo. |
| 4 | `16` | EEPROM | Memoria no volátil disponible. |
| 5 | `32` | DAC | Salida analógica real (True DAC). |
| 6 | `64` | HW Serial 1 | Segundo puerto serial hardware disponible. |
| 7 | `128` | FPU | Unidad de punto flotante hardware. |
| 8 | `256` | 3.3V Logic | Niveles lógicos de 3.3V (vs 5V). |
| 9 | `512` | Big Buffer | Buffer RX serial extendido (>64 bytes). |
| 10 | `1024` | I2C | Soporte hardware I2C (Wire/SDA/SCL). |
| 11 | `2048` | SPI | Soporte hardware SPI (SCK/MOSI/MISO). |
| 12 | `4096` | SD | Tarjeta SD física detectada y funcional. |

---

## 7. Erradicación de Compresión RLE (Arquitectura v3)

A partir de la versión 2.8.5, **RLE ha sido eliminado del protocolo**.
- **Razón:** Protobuf v3 mitiga la necesidad de compresión manual mediante el uso de **Varints** (los ceros y valores pequeños ocupan menos bytes) y la omisión de campos por defecto.
- **Beneficio:** Reducción de latencia de CPU en el MCU y eliminación de buffers temporales, incrementando la seguridad funcional (SIL-2).

---

## 8. Fallback & Degradation Behavior

(Mantener sección original de fallbacks y troubleshooting...)
