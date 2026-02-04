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

La **fuente de verdad machine-readable** del protocolo vive en `tools/protocol/spec.toml`.

Centralizar en el spec significa **solo** lo que debe ser idéntico entre implementaciones (MCU y Linux) o lo que constituye el contrato externo (MQTT) que otros clientes consumen.

Qué **sí** se centraliza en `spec.toml` (y se genera a Python/C++):

- **Contrato wire MCU↔Linux**: enums/IDs (`Command`, `Status`), layouts de payload, límites, sentinels/máscaras, formatos `struct` y cualquier valor validado por ambos lados.
- **Handshake autenticado**: formato serializado y rangos (`HANDSHAKE_CONFIG_*`, `*_MIN/MAX`) y defaults que formen parte del intercambio/validación.
- **Contrato MQTT público**: prefijo por defecto, sufijos y tokens canónicos que impactan interoperabilidad (`MQTT_DEFAULT_TOPIC_PREFIX`, `MQTT_SUFFIX_*`, `STATUS_REASON_*`).

Este documento **no duplica listados enumerados** (por ejemplo `[mqtt_suffixes]`, `[status_reasons]`, `[[mqtt_subscriptions]]`, `[[topics]]`, `[[actions]]`) para evitar drift; esos catálogos se consideran canónicos en el spec y en los bindings generados.

Qué **no** se centraliza en el spec (porque es decisión de despliegue/runtime):

- **Defaults de OpenWrt/daemon**: `DEFAULT_MQTT_HOST`, `DEFAULT_MQTT_PORT`, rutas `/tmp`, spool dir, límites de colas del daemon, parámetros del exporter/metrics, timeouts en segundos de tareas del daemon, etc. Esto vive en UCI y en `openwrt-mcu-bridge/mcubridge/const.py`.

Al ejecutar:

- `python3 tools/protocol/generate.py --spec tools/protocol/spec.toml --py openwrt-mcu-bridge/mcubridge/rpc/protocol.py --cpp openwrt-library-arduino/src/protocol/rpc_protocol.h`

…se regeneran los bindings de Python y C++ y deben commitearse en el mismo cambio.

Este documento actúa además como **contrato normativo** de direccionalidad y semántica (ACK/RESP). Si el comportamiento real difiere, se considera un bug: hay que ajustar implementación + `spec.toml` + este documento.

---

# Arquitectura

Esta sección resume cómo se articula el daemon, qué garantías de seguridad ofrece y cómo se observan los flujos críticos.

## Componentes

- **BridgeService (Python 3.13.9-r2)**: orquesta la comunicación MCU↔Linux, aplica políticas de topics y delega en componentes (`FileComponent`, `ProcessComponent`, etc.).
- **RuntimeState**: mantiene el estado mutable (colas MQTT, handshake, spool, métricas) y expone snapshots consistentes para status, MQTT y Prometheus.
- **High-Performance Transport**: El daemon utiliza `pyserial-asyncio-fast` para minimizar la latencia y evitar el doble buffering en el enlace serie.
- **MQTT Publisher**: publica respuestas/telemetría con MQTT v5 (correlation data, response_topic, expiración, metadatos).
- **MCU Firmware (openwrt-library-arduino)**: implementa el protocolo binario bajo normativa SIL-2 y vela por el secreto compartido del enlace serie.
- **Instrumentación**: el daemon escribe `/tmp/mcubridge_status.json` (snapshot en tmpfs; se pierde al reboot), publica métricas en `br/system/metrics` y puede exponer Prometheus por HTTP.

## Seguridad

1. **TLS recomendado**: `mqtt_tls=1` por defecto y se exige `mqtt_cafile` para levantar TLS. TLS puede desactivarse para depuración, pero el daemon lo registra como advertencia.
2. **Derivación de Claves (HKDF)**: Se utiliza **HKDF-SHA256 (RFC 5869)** para derivar la clave de autenticación del handshake a partir del `serial_shared_secret`. Esto evita el uso directo del secreto en el bus y proporciona aislamiento criptográfico.
3. **Secreto serie fuerte**: handshake MCU↔Linux con nonce de 16 bytes y `HMAC-SHA256(derived_key, nonce)` truncado a 16 bytes. El daemon rechaza el placeholder `changeme123`.
4. **Auto-Validación (KAT)**: Tanto el MCU como el daemon realizan **Known Answer Tests (KAT)** para SHA256 y HMAC-SHA256 en cada arranque. Si la validación falla, el sistema entra en modo **Fail-Secure** y aborta la operación.
5. **Lista blanca de comandos**: `allowed_commands` se normaliza en `AllowedCommandPolicy` y se aplica al ejecutar procesos/shell.
4. **Topics sensibles**: `TopicAuthorization` gobierna toggles `mqtt_allow_*` (consola, archivos, datastore, mailbox, shell, GPIO, etc.).
5. **Sandbox de archivos**: `FileComponent` normaliza rutas, bloquea `..`, obliga a permanecer bajo `file_system_root`, aplica `file_write_max_bytes` y `file_storage_quota_bytes`.

## Observabilidad

- **Logging estructurado**: logs JSON (`ts`, `level`, `logger`, `message`, `extra`) enviados a syslog.
- **Destino de logs**: Por defecto OpenWrt usa `logread` (ring buffer en RAM), NO escribe a `/var/log/` en flash.
- **Metrics MQTT**: snapshots periódicos en `br/system/metrics`.
- **Exportador Prometheus**: opcional (por defecto `127.0.0.1:9130`). Campos no numéricos se exponen como `*_info{...} 1`.
- **Status Writer**: `/tmp/mcubridge_status.json` como snapshot local (tmpfs/RAM).

## Máquina de Estados (ETL FSM) — IEC 61508 / SIL 2

El sistema de comunicación MCU↔Linux implementa una **máquina de estados finita (FSM)** basada en la biblioteca [ETL (Embedded Template Library)](https://www.etlcpp.com/fsm.html) `etl::fsm`. Esta arquitectura garantiza transiciones deterministas y trazables, cumpliendo con IEC 61508 (SIL-2).

### Estados

| Estado | ID | Descripción |
|--------|----|-------------|
| `StateUnsynchronized` | 0 | Enlace no establecido. Esperando handshake. |
| `StateIdle` | 1 | Enlace sincronizado, listo para operar. |
| `StateAwaitingAck` | 2 | Frame crítico enviado, esperando confirmación. |
| `StateFault` | 3 | Fallo criptográfico detectado. Estado terminal. |

### Eventos

| Evento | Descripción |
|--------|-------------|
| `EvHandshakeComplete` | Handshake HMAC validado exitosamente. |
| `EvSendCritical` | Frame que requiere ACK enviado. |
| `EvAckReceived` | ACK recibido para frame pendiente. |
| `EvTimeout` | Timeout de ACK (retries agotados). |
| `EvReset` | Solicitud de reset del enlace. |
| `EvCryptoFault` | Fallo de integridad criptográfica (KAT fallido). |

### Diagrama de Transiciones

```
                    ┌─────────────────────┐
                    │   Unsynchronized    │◄────────────────┐
                    │        (0)          │                 │
                    └──────────┬──────────┘                 │
                               │ EvHandshakeComplete        │ EvReset
                               ▼                            │
                    ┌─────────────────────┐                 │
              ┌────►│        Idle         │─────────────────┤
              │     │        (1)          │                 │
              │     └──────────┬──────────┘                 │
              │                │ EvSendCritical             │
              │                ▼                            │
              │     ┌─────────────────────┐                 │
 EvAckReceived│     │    AwaitingAck      │─────────────────┘
              │     │        (2)          │
              │     └──────────┬──────────┘
              │                │ EvTimeout
              └────────────────┘

                    ┌─────────────────────┐
      EvCryptoFault │       Fault         │ (estado terminal)
         ──────────►│        (3)          │
                    └─────────────────────┘
```

### Implementación

El FSM está implementado en `src/fsm/bridge_fsm.h` usando el framework `etl::fsm`:

```cpp
#include "fsm/bridge_fsm.h"

// Consulta de estado
bool synced = _fsm.isIdle() || _fsm.isAwaitingAck();
bool waiting = _fsm.isAwaitingAck();
bool failed = _fsm.isFault();

// Transiciones via eventos
_fsm.handshakeComplete();  // Unsynchronized → Idle
_fsm.sendCritical();       // Idle → AwaitingAck
_fsm.ackReceived();        // AwaitingAck → Idle
_fsm.timeout();            // AwaitingAck → Idle
_fsm.reset();              // Any → Unsynchronized
_fsm.cryptoFault();        // Any → Fault
```

### Ventajas sobre enum + switch

| Aspecto | Enum | ETL FSM |
|---------|------|---------|
| Validación de transiciones | Manual | Automática |
| Trazabilidad | Difícil | `etl::fsm_state_id_t` |
| Extensibilidad | Cambios dispersos | On-enter/on-exit hooks |
| Testabilidad | Estado global | Estado encapsulado |
| Conformidad SIL-2 | Requiere evidencia manual | Determinístico por diseño |

---

## Estado Seguro (Fail-Safe State) — IEC 61508 / SIL 2

Esta sección documenta formalmente el comportamiento del sistema ante condiciones de fallo, cumpliendo con los requisitos de Seguridad Funcional.

### Definición de Estado Seguro

El **Estado Seguro** es la configuración a la que el sistema transiciona automáticamente cuando detecta un fallo irrecuperable. En este estado:

1. **GPIO**: Todos los pines configurados por el Bridge se resetean a `INPUT` (alta impedancia), evitando actuaciones no intencionadas.
2. **Comunicación Serial**: El enlace RPC se considera no sincronizado (`_synchronized = false`).
3. **Colas pendientes**: Se vacían todas las colas TX/RX para evitar procesamiento de datos corruptos.
4. **Flow Control**: Se libera cualquier estado XOFF para evitar deadlocks.

### Matriz de Transición a Estado Seguro

| Condición de Fallo | Acción MCU | Acción Daemon | Estado Resultante |
|--------------------|------------|---------------|-------------------|
| CRC Mismatch | Reset parser, emit `STATUS_CRC_MISMATCH` | Log + reintento | Link degradado |
| Frame Malformed | Reset parser, emit `STATUS_MALFORMED` | Log + descarte | Link operativo |
| Buffer Overflow | Reset parser, descarte silencioso | Log + descarte | Link operativo |
| Handshake Timeout | `enterSafeState()` | Backoff exponencial | Link no sincronizado |
| Handshake Auth Fail | `enterSafeState()` | `SerialHandshakeFatal` si > N fallos | Link rechazado |
| Serial Disconnect | N/A (hardware) | Clear queues, `serial_tx_allowed.set()` | Reconexión pendiente |
| ACK Timeout (max retries) | `_awaiting_ack = false` | Log + siguiente frame | Frame perdido |
| Watchdog Timeout | Reset MCU (AVR `wdt_reset()`) | Depende de procd | Reinicio completo |

### Invariantes de Seguridad

1. **No hay alocación dinámica post-inicialización**: Todos los buffers son estáticos con tamaños conocidos en compile-time.
2. **No hay recursión**: Todas las funciones usan iteración para evitar stack overflow.
3. **Validación de rangos**: Cada entrada externa se valida contra límites antes de uso.
4. **CRC obligatorio**: Ningún frame se procesa sin verificación CRC exitosa.
5. **Timeout en todas las operaciones bloqueantes**: Previene deadlocks indefinidos.

### Recuperación Automática

El sistema implementa recuperación automática con backoff exponencial:

```
Handshake retry: base=1s, max=60s, factor=2x
Serial reconnect: base=reconnect_delay (UCI), max=8x base
MQTT spool retry: base=5s, max=60s
```

### Monitoreo de Estado Seguro

El estado de salud del enlace se expone en:
- `/tmp/mcubridge_status.json` → campo `link_is_synchronized`
- MQTT topic `br/system/bridge/summary/value` → snapshot completo
- Prometheus metric `mcubridge_serial_link_synchronized` (si habilitado)

## Configuración relevante

| Clave | Descripción | Valor por defecto |
| --- | --- | --- |
| `metrics_enabled` | Activa exportador Prometheus. | `0` |
| `metrics_host` | Dirección de enlace para exportador. | `127.0.0.1` |
| `metrics_port` | Puerto TCP del exportador. | `9130` |
| `debug_logging` | Fuerza logs `DEBUG`. | `0` |
| `allowed_commands` | Lista blanca de comandos shell. | `""` |
| `file_write_max_bytes` | Máximo por write (MQTT y/o `CMD_FILE_WRITE`). | `262144` |
| `file_storage_quota_bytes` | Cuota global dentro de `file_system_root`. | `4194304` |

## Flujo de inicio (resumen)

1. `main()` carga config, inicializa logging, crea `RuntimeState`.
2. Se arranca un `TaskGroup` con lector serie, MQTT, status writer, métricas MQTT, watchdog opcional, Prometheus opcional.
3. Fallas críticas se elevan como `CRITICAL` para reinicios supervisados (`procd`).

---

## Notas de plataforma (Arduino MCU)

### Consola del kernel y conflicto con ttyATH0

El Arduino MCU presenta un conflicto de hardware: el puerto serial `/dev/ttyATH0` es usado simultáneamente por:

1. **Consola del kernel** (configurada en bootargs a 250000 baud)
2. **Protocolo McuBridge** (opera a 115200 baud)

Aunque los baud rates difieren, los mensajes `printk` del kernel pueden corromper frames COBS del protocolo, causando errores de parsing como:

```
Frame parse error: payload_length=... cmd_id=... (COBS decode failed)
```

**Solución automática**: El paquete `openwrt-mcu-core` incluye el script UCI-defaults `95-mcubridge-silence-kernel-console` que:

1. Crea `/etc/sysctl.d/99-mcubridge-no-console.conf` con `kernel.printk = 0 0 0 0`
2. Añade un respaldo en `/etc/rc.local`

Esto silencia los mensajes del kernel en la consola serial sin recompilar el kernel ni modificar U-Boot.

**Verificación manual**:
```bash
# Ver estado actual
cat /proc/sys/kernel/printk

# Silenciar temporalmente
echo 0 > /proc/sys/kernel/printk
dmesg -n 1
```

**Nota**: Si se necesita debug del kernel, se puede acceder vía `dmesg` o `logread` sin afectar el protocolo serial.

---

# Protocolo binario (RPC)

## 1. Visión general

Este protocolo binario se usa entre microcontrolador (MCU) y procesador Linux (MPU). Refleja el comportamiento publicado en el daemon y el firmware.

La comunicación sigue un modelo de RPC: normalmente el MPU inicia peticiones y el MCU responde, pero existen comandos **bidireccionales** (por ejemplo consola/mailbox/archivo) que se interpretan como “push simétrico”.

## 1.1 Contrato de direccionalidad y ACK

### Tipos de mensajes

- **Request/Response**: un emisor envía `CMD_X` y el otro contesta con `CMD_X_RESP`.
- **ACK-only (fire-and-forget)**: el emisor envía `CMD_X` y el receptor contesta con `STATUS_ACK` (no existe `CMD_X_RESP`).
- **Push simétrico**: el mismo `CMD_X` puede viajar en ambas direcciones con el mismo layout de payload; semánticamente es “entrega de datos al otro extremo”. En este caso se usa `STATUS_ACK` para confirmación.

### Semántica de `STATUS_ACK`

`STATUS_ACK` (`0x38`) confirma recepción de un comando que requiere ACK.

- Payload recomendado: `ack_id: u16` (Big Endian), igual al `command_id` original.
- Si el payload no incluye `ack_id`, el receptor puede inferir el ACK como genérico (pero se recomienda siempre incluirlo).

### Comandos que requieren ACK

Hay dos niveles distintos de “ACK” en este sistema:

1) **ACK de transporte (recomendado por compatibilidad):** tras procesar exitosamente un frame *no-status* recibido, el receptor responde con `STATUS_ACK (0x38)` para confirmar recepción (y permitir retries acotados si el emisor no ve ese ACK).
  - Excepciones: frames de `Status` y `CMD_XOFF`/`CMD_XON`.
  - Si el receptor detecta un error de framing/semántica, puede responder con `STATUS_MALFORMED`, `STATUS_ERROR`, etc.

2) **Comandos “ACK-only” (sin respuesta de negocio):** son comandos para los cuales **no existe** `CMD_X_RESP`; el éxito se confirma con `STATUS_ACK`.
  Esta lista está alineada con el spec (bindings Python: `ACK_ONLY_COMMANDS`):

- `CMD_SET_PIN_MODE`, `CMD_DIGITAL_WRITE`, `CMD_ANALOG_WRITE` (Linux → MCU)
- `CMD_CONSOLE_WRITE` (bidireccional)
- `CMD_DATASTORE_PUT` (MCU → Linux)
- `CMD_MAILBOX_PUSH` (bidireccional)
- `CMD_FILE_WRITE` (bidireccional)

## 2. Transporte

Los frames se encapsulan con **Consistent Overhead Byte Stuffing (COBS)** y cada frame codificado termina en `0x00` (definido como `RPC_FRAME_DELIMITER`).

- `MAX_PAYLOAD_SIZE = 128` bytes.
- Todos los enteros multi-byte se codifican en **Big Endian**.

## 3. Formato de frame (antes de COBS)

```
+--------------------------+---------------------+----------+
| Cabecera (5 bytes)       | Payload (0-128)     | CRC32    |
+--------------------------+---------------------+----------+
```

### 3.1 Cabecera

| Campo | Tipo | Descripción |
| --- | --- | --- |
| `version` | `uint8_t` | Versión del protocolo (actual: `0x02`). |
| `payload_length` | `uint16_t` | Longitud del payload en bytes. |
| `command_id` | `uint16_t` | Identificador del comando o status. |

### 3.2 CRC

CRC32 (4 bytes, Big Endian) sobre cabecera+payload; CRC-32 IEEE 802.3 con polinomio reflejado `0xEDB88320`, estado inicial `0xFFFFFFFF`, XOR final `0xFFFFFFFF`.

### 3.3 Implementación de bibliotecas (Wire Format)

| Componente | Función | Implementación MCU (Arduino/C++) | Implementación Daemon (Python) |
| :--- | :--- | :--- | :--- |
| **COBS** | Framing / Escaping | **Interna**: `protocol/cobs.h` (cero dependencias externas, reemplaza `PacketSerial`). | **Externa**: `cobs` (paquete PyPI) o implementación interna en `mcubridge.transport`. |
| **CRC32** | Integridad | **Interna**: `etl::crc32` (ETL SIL-2 certified). | **Interna**: `binascii.crc32` (IEEE 802.3 standard). |
| **Endianness** | Byte Order | `__builtin_bswap16/32` o macros custom. | `struct.pack('>...')` (Big Endian standard library). |

## 4. Códigos de estado (`Status`)

Los status usan el rango `0x30` - `0x3F`.

| Código | Nombre | Uso típico |
| --- | --- | --- |
| `0x30` | `STATUS_OK` | Operación completada correctamente. |
| `0x31` | `STATUS_ERROR` | Fallo genérico. |
| `0x32` | `STATUS_CMD_UNKNOWN` | Comando no reconocido. |
| `0x33` | `STATUS_MALFORMED` | Payload inválido. |
| `0x34` | `STATUS_OVERFLOW` | Frame excede buffers. |
| `0x35` | `STATUS_CRC_MISMATCH` | CRC inválido. |
| `0x36` | `STATUS_TIMEOUT` | Timeout. |
| `0x37` | `STATUS_NOT_IMPLEMENTED` | Definido pero no soportado. |
| `0x38` | `STATUS_ACK` | ACK para operaciones fire-and-forget/push simétrico. |

## 5. Comandos

## 5.0 Semántica de ACK, retries e idempotencia (importante)

Algunos comandos son **fire-and-forget** (no tienen respuesta “de negocio”) pero sí requieren confirmación mediante `STATUS_ACK (0x38)`.

- **Retries por ACK perdido:** Linux puede reenviar el mismo frame si no recibe `STATUS_ACK` dentro de `ack_timeout_ms` (configurable via `CMD_LINK_RESET`).
- **Idempotencia (mejor esfuerzo, sin `tx_id`):** el protocolo actual no incluye un identificador de transmisión en el header; por eso la idempotencia estricta no puede garantizarse sólo con el wire-format.
  - El firmware (MCU) implementa deduplicación de **reintentos** para comandos con side-effects (p. ej. consola, mailbox, file write, GPIO write/mode): si llega un duplicado **después** de `ack_timeout_ms` y **antes** del fin del horizonte de retries, el MCU reenvía `STATUS_ACK` pero **no re-ejecuta** el handler.
  - Para reducir falsos positivos, un frame idéntico recibido **antes** del `ack_timeout_ms` se trata como un comando nuevo (p. ej. repeticiones legítimas a alta frecuencia).

Notas:
- Esta deduplicación es deliberadamente conservadora: protege contra retries por ACK perdido sin “romper” casos de uso de repetición rápida.
- Si se requiere idempotencia inequívoca para todos los casos, el camino correcto es introducir un `tx_id` (implica versionado del protocolo y regeneración Python/C++).

### 5.1 Sistema y control de flujo (0x40 – 0x4F)

- **`0x40` CMD_GET_VERSION (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x41 CMD_GET_VERSION_RESP`): `[version_major: u8, version_minor: u8]`.

- **`0x42` CMD_GET_FREE_MEMORY (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x43 CMD_GET_FREE_MEMORY_RESP`): `[free_memory: u16]`.

- **`0x44` CMD_LINK_SYNC (Linux → MCU)**
  - Petición: `nonce: byte[16]`.
  - Respuesta (`0x45 CMD_LINK_SYNC_RESP`, MCU → Linux): `nonce || tag`.

- **`0x46` CMD_LINK_RESET (Linux → MCU)**
  - Petición: opcionalmente `[ack_timeout: u16, retry_limit: u8, response_timeout: u32]`.
  - Respuesta (`0x47 CMD_LINK_RESET_RESP`): sin payload.

- **`0x4A` CMD_SET_BAUDRATE (Linux → MCU)**
  - Petición: `[baudrate: u32]`.
  - Respuesta (`0x4B CMD_SET_BAUDRATE_RESP`): sin payload.

- **`0x48` CMD_GET_CAPABILITIES (Linux → MCU)**
  - Petición: sin payload.
  - Respuesta (`0x49 CMD_GET_CAPABILITIES_RESP`): `[proto_ver: u8, arch: u8, num_digital: u8, num_analog: u8, features: u32]`.
  - **Propósito:** Introspección estática de hardware (SIL-2 Safety). Permite al MPU validar que la configuración de pines no excede los límites físicos del MCU.
  - **Features Bitmask (u32):**
    | Bit | Valor | Feature | Descripción |
    | :--- | :--- | :--- | :--- |
    | 0 | `1` | Watchdog | MCU Watchdog habilitado. |
    | 1 | `2` | RLE | Compresión RLE soportada. |
    | 2 | `4` | Debug Frames | Logging de tramas activo. |
    | 3 | `8` | Debug IO | Logging de GPIO activo. |
    | 4 | `16` | EEPROM | Memoria no volátil disponible. |
    | 5 | `32` | DAC | Salida analógica real (True DAC). |
    | 6 | `64` | HW Serial 1 | Segundo puerto serial hardware disponible. |
    | 7 | `128` | FPU | Unidad de punto flotante hardware. |
    | 8 | `256` | 3.3V Logic | Niveles lógicos de 3.3V (vs 5V). |
    | 9 | `512` | Big Buffer | Buffer RX serial extendido (>64 bytes). |
    | 10 | `1024` | I2C | Soporte hardware I2C (Wire/SDA/SCL). |

- **`0x4E` CMD_XOFF (MCU → Linux)** / **`0x4F` CMD_XON (MCU → Linux)**
  - Sin payload.
  - Semántica: `CMD_XOFF` indica backpressure del MCU (buffers/colas cerca de saturación). Linux debe **detener todo envío** de frames al MCU hasta recibir `CMD_XON`.
  - Implementación esperada en daemon: el gating de TX es global (no sólo consola), y debe liberarse también ante desconexión para evitar deadlocks.

### 5.1.1 Control de Flujo XON/XOFF (Detalle)

El protocolo implementa control de flujo por software para proteger los buffers limitados del MCU (típicamente 64-256 bytes de RX en AVR).

#### Mecanismo

```
MCU detecta RX buffer > 75% → envía CMD_XOFF (0x4E) → Linux pausa TX
MCU detecta RX buffer < 25% → envía CMD_XON (0x4F)  → Linux reanuda TX
```

#### Parámetros de Configuración (MCU)

| Macro | Valor por defecto | Descripción |
| --- | --- | --- |
| `BRIDGE_HW_RX_BUFFER_SIZE` | 64 | Tamaño asumido del buffer RX hardware |
| `BRIDGE_RX_HIGH_WATER_MARK` | 75% (48 bytes) | Umbral para emitir XOFF |
| `BRIDGE_RX_LOW_WATER_MARK` | 25% (16 bytes) | Umbral para emitir XON |

#### Comportamiento del Daemon (Linux)

1. **Recepción de XOFF**: El daemon detiene inmediatamente todo envío de frames hacia el MCU.
2. **Recepción de XON**: El daemon reanuda el envío normal de frames.
3. **Desconexión**: Al detectar desconexión del puerto serial, el estado de pausa se limpia automáticamente para evitar deadlocks.
4. **Timeout**: Si el daemon permanece en estado XOFF por más de `response_timeout_ms`, puede considerar el enlace como degradado.

#### Notas de Implementación

- `CMD_XOFF` y `CMD_XON` **no requieren ACK** (`requires_ack = false` en spec.toml).
- Son comandos unidireccionales MCU → Linux únicamente.
- El daemon debe aplicar el gating de forma **global** (todos los comandos, no solo consola).
- La pausa debe liberarse inmediatamente ante pérdida de conexión serial.

### 5.2 GPIO (0x50 – 0x5F)

- **`0x50` CMD_SET_PIN_MODE (Linux → MCU)**: `[pin: u8, mode: u8]`.
- **`0x51` CMD_DIGITAL_WRITE (Linux → MCU)**: `[pin: u8, value: u8]`.
- **`0x52` CMD_ANALOG_WRITE (Linux → MCU)**: `[pin: u8, value: u8]`.
- **`0x53` CMD_DIGITAL_READ (Linux → MCU)**: `[pin: u8]`. Respuesta `0x55 CMD_DIGITAL_READ_RESP`: `[value: u8]`.
- **`0x54` CMD_ANALOG_READ (Linux → MCU)**: `[pin: u8]`. Respuesta `0x56 CMD_ANALOG_READ_RESP`: `[value: u16]`.

### 5.3 Consola (0x60)

- **`0x60` CMD_CONSOLE_WRITE (bidireccional)**
  - Payload: `chunk: byte[]` (máx. 128 bytes).
  - Confirmación: `STATUS_ACK (0x38)`.

### 5.4 Datastore (0x70)

- **`0x70` CMD_DATASTORE_PUT (MCU → Linux)**: `[key_len: u8, key: char[], value_len: u8, value: char[]]`.
- **`0x71` CMD_DATASTORE_GET (MCU → Linux)**: `[key_len: u8, key: char[]]`.
- **`0x72` CMD_DATASTORE_GET_RESP (Linux → MCU)**: `[value_len: u8, value: char[]]`.

Notas operativas:

- El datastore del daemon es **volátil (RAM)**: se mantiene en memoria mientras el proceso está vivo y **no persiste a disco**.
- La persistencia “durable” del sistema se limita al spool MQTT (si está habilitado) y por defecto se ubica en `/tmp` para minimizar desgaste de flash.

### 5.5 Mailbox (0x80)

- **`0x80` CMD_MAILBOX_READ (MCU → Linux)**: sin payload. Respuesta `0x84 CMD_MAILBOX_READ_RESP`.
- **`0x81` CMD_MAILBOX_PROCESSED (MCU → Linux)**: `[message_id: u16]` (opcional).
- **`0x82` CMD_MAILBOX_AVAILABLE (MCU → Linux)**:
  - Modo *request*: sin payload.
  - Respuesta: `0x85 CMD_MAILBOX_AVAILABLE_RESP` (Linux → MCU) con `[count: u8]`.
  - Regla estricta: si el request incluye payload (cualquier `payload_length != 0`), el daemon responde con `STATUS_MALFORMED (0x33)` cuyo payload es el `command_id` (u16 big-endian) del request.
  - `CMD_MAILBOX_AVAILABLE` (Linux → MCU) es inválido en el contrato y el firmware lo descarta.
- **`0x83` CMD_MAILBOX_PUSH (push simétrico, bidireccional)**:
  - Payload: `[message_len: u16, message: byte[]]`.
  - Confirmación: `STATUS_ACK (0x38)`.

### 5.6 Sistema de archivos (0x90)

- **`0x90` CMD_FILE_WRITE (push simétrico, bidireccional)**:
  - Payload: `[path_len: u8, path: char[], data_len: u16, data: byte[]]`.
  - Confirmación: `STATUS_ACK (0x38)`.
- **`0x91` CMD_FILE_READ (MCU → Linux)**: `[path_len: u8, path: char[]]`. Respuesta `0x93 CMD_FILE_READ_RESP`.
- **`0x92` CMD_FILE_REMOVE (MCU → Linux)**: `[path_len: u8, path: char[]]`.

### 5.7 Gestión de procesos (0xA0)

- **`0xA0` CMD_PROCESS_RUN (MCU → Linux)**: `command: char[]`.
- **`0xA1` CMD_PROCESS_RUN_ASYNC (MCU → Linux)**: `command: char[]`.
- **`0xA2` CMD_PROCESS_POLL (MCU → Linux)**: `[process_id: u16]`.
- **`0xA3` CMD_PROCESS_KILL (MCU → Linux)**: `[process_id: u16]`.

Respuestas (Linux → MCU):

- **`0xA4` CMD_PROCESS_RUN_RESP (Linux → MCU)**: respuesta al `CMD_PROCESS_RUN`.
- **`0xA5` CMD_PROCESS_RUN_ASYNC_RESP (Linux → MCU)**: respuesta al `CMD_PROCESS_RUN_ASYNC`.
- **`0xA6` CMD_PROCESS_POLL_RESP (Linux → MCU)**: respuesta al `CMD_PROCESS_POLL`.

Notas:
- El wire-format exacto de payload está definido en `tools/protocol/spec.toml` y se refleja en los bindings generados.
- `CMD_PROCESS_KILL` se confirma típicamente con status/ACK a nivel de transporte.

## 6. Consideraciones adicionales

- **Truncado**: si una respuesta supera `MAX_PAYLOAD_SIZE`, los datos se truncan.
- **MQTT**: además del RPC serie, el daemon expone una API MQTT.
  - Dirección: MQTT clientes → daemon (comandos), daemon → MQTT (respuestas/snapshots).
  - La lista de suscripciones (incluyendo comodines y QoS) vive en `tools/protocol/spec.toml` (`[[mqtt_subscriptions]]`) y se genera a Python como `MQTT_COMMAND_SUBSCRIPTIONS`.

---

## 7. Compresión RLE (opcional)

El protocolo incluye una implementación de **Run-Length Encoding (RLE)** optimizada para sistemas embebidos con RAM limitada. Está disponible en ambos lados (MCU y daemon) para comprimir payloads antes de transmitir.

### 7.1 Casos de uso

| Tipo de dato | Compresión típica | Recomendación |
| --- | --- | --- |
| Console output con espacios/tabs | 1.2x - 2x | ✅ Usar |
| Datos de sensores repetitivos | 2x - 10x | ✅ Usar |
| Archivos con padding nulo | 10x - 50x | ✅ Usar |
| Datos GPIO (1-2 bytes) | N/A | ❌ No usar |
| Handshake/crypto | N/A | ❌ No usar |
| Datos aleatorios | ~1x | ❌ No usar |

### 7.2 Formato de codificación

```
Literal:     byte                    (si byte ≠ 0xFF)
Run:         0xFF <count> <byte>     (secuencia de bytes repetidos)
```

| Count | Significado |
| --- | --- |
| 0-254 | Run length = count + 2 (2-256 bytes) |
| 255 | Marcador especial: exactamente 1 byte (para escapar 0xFF aislado) |

**Ejemplos:**

```
Input:   "AAAAA"           (5 bytes)
Output:  0xFF 0x03 0x41    (3 bytes: escape + count=3+2=5 + 'A')

Input:   0xFF              (1 byte aislado)
Output:  0xFF 0xFF 0xFF    (3 bytes: escape + marcador_especial + 0xFF)

Input:   0xFF 0xFF         (2 bytes consecutivos)
Output:  0xFF 0x00 0xFF    (3 bytes: escape + count=0+2=2 + 0xFF)
```

### 7.3 Parámetros

| Constante | Valor | Descripción |
| --- | --- | --- |
| `RLE_ESCAPE_BYTE` | `0xFF` | Byte que indica inicio de secuencia codificada |
| `RLE_MIN_RUN_LENGTH` | 4 | Longitud mínima para codificar (break-even) |
| `RLE_MAX_RUN_LENGTH` | 256 | Máximo por secuencia (runs más largos se dividen) |

### 7.4 Performance

| Escenario | Input | Output | Ratio |
| --- | --- | --- | --- |
| Datos uniformes | 100 bytes | 3 bytes | **33x** |
| Largo uniforme | 1000 bytes | 12 bytes | **83x** |
| Texto normal | 100 bytes | ~100 bytes | ~1x |
| Muchos 0xFF aislados | 50 bytes | ~150 bytes | 0.3x (expansión) |

### 7.5 Uso en código

**C++ (MCU):**
```cpp
#include "protocol/rle.h"

uint8_t input[] = "AAAAAAAAAA";  // 10 bytes
uint8_t output[32];

// Verificar si vale la pena comprimir
if (rle::should_compress(input, 10)) {
  size_t compressed_len = rle::encode(input, 10, output, sizeof(output));
  // compressed_len = 3 bytes
}

// Decodificar
uint8_t decoded[32];
size_t decoded_len = rle::decode(output, compressed_len, decoded, sizeof(decoded));
```

**Python (daemon):**
```python
from mcubridge.rpc.rle import encode, decode, should_compress

data = b"A" * 100

if should_compress(data):
    compressed = encode(data)  # 3 bytes
    original = decode(compressed)  # 100 bytes
```

### 7.6 Recursos

- **RAM (MCU):** ~10 bytes de stack, sin heap
- **Flash (MCU):** ~500 bytes de código
- **Archivos fuente:**
  - C++: `openwrt-library-arduino/src/protocol/rle.h`
  - Python: `openwrt-mcu-bridge/mcubridge/rpc/rle.py`
  - Spec: `tools/protocol/spec.toml` (sección `[compression]`)

---

### MQTT: snapshots del bridge (SYSTEM/bridge/*)

Además de los comandos anteriores, el daemon expone endpoints de lectura de estado:

- `br/system/bridge/handshake/get` → publica `br/system/bridge/handshake/value`.
- `br/system/bridge/summary/get` → publica `br/system/bridge/summary/value`.
- `br/system/bridge/state/get` → publica `br/system/bridge/summary/value` (alias histórico).

Estos topics forman parte del contrato operativo y deben estar definidos en `tools/protocol/spec.toml`.
