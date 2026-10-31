# Plan de Migración de Protocolo a Google Pigweed

- **Documento:** Especificación y Hoja de Ruta de Migración
- **Módulo Afectado:** Protocolo de Transporte y RPC MCU-MPU
- **Estado:** Propuesta Técnica Aprobada (Fase de Planificación e Implementación)
- **Base Tecnológica MCU:** [`isantolin/avr_pigweed_port`](https://github.com/isantolin/avr_pigweed_port) (Commit `dd318aa`)
- **Estrategia de Integración Seleccionada:** **Opción A (Git Submodule)** en `third_party/avr_pigweed_port`

---

## 1. Motivación y Diagnóstico del Protocolo Actual

El protocolo actual de `arduino-yun-bridge2` utiliza una arquitectura ad-hoc:
1. **Framing Monocanal COBS (Consistent Overhead Byte Stuffing):** Un solo canal de bytes delimitado por `0x00`. Todos los mensajes (llamadas RPC, respuestas, streams de consola, errores y telemetría) deben empaquetarse en un único tipo de sobre `RpcEnvelope`.
2. **Despacho Estático por Switch:** `tools/protocol/generate.py` genera un archivo de inclusión [`rpc_dispatch_table.inc`](file:///home/ignaciosantolin/arduino-yun-bridge2/mcubridge-library-arduino/src/protocol/rpc_dispatch_table.inc) que contiene un `switch (command_id)` gigante. Si un mensaje nuevo no está contemplado o hay un desfase de sincronización, la recuperación es compleja.
3. **Suscripción de Pines Asíncrona Frágil:** Al no existir el concepto de *streaming RPC*, el MCU envía notificaciones asíncronas inyectando tramas no solicitadas que el daemon debe demultiplexar manualmente con identificadores de correlación especiales.
4. **Consumo de Memoria Flash por Strings:** En el ATmega2560 (AVR de 8 bits), las cadenas de texto literales consumen memoria de programa valiosa (o SRAM si se omiten directivas `F()` / `PROGMEM`).

### La Solución con Google Pigweed:
* **Framing Multicanal HDLC (`pw_hdlc`):** Permite multiplexar por hardware/software tres canales lógicos independientes sobre el mismo par UART:
  * **Canal 1 (Dirección `0x01`):** `pw_rpc` (Servicios RPC: GPIO, SPI, Mailbox, Process, FileSystem, Bridge).
  * **Canal 2 (Dirección `0x02`):** `pw_log` (Logs tokenizados del MCU y diagnósticos de pánico).
  * **Canal 3 (Dirección `0x03`):** `pw_stream` (Consola bidireccional transparente).
* **Estandarización gRPC (`pw_rpc`):** Definición formal de servicios `.proto` con generación automática de stubs de cliente en Python y servidor en C++.
* **Server Streaming RPC Nativo:** El método `SubscribePins` devuelve `stream PinNotification`, gestionado de forma nativa por el framework con control de flujo y ciclo de vida de llamada.
* **Tokenized Logging (`pw_tokenizer`):** En tiempo de compilación, cada formato de string se convierte en un hash de 32 bits (FNV-1a). El MCU solo transmite **4 bytes de token más argumentos varint**, ahorrando más de **8 a 12 KB de Flash** en el ATmega2560.

---

## 2. Diagrama de Arquitectura del Sistema

```mermaid
sequenceDiagram
    autonumber
    participant Host as OpenWrt MPU (mcubridge)
    participant Demux as pw_hdlc Demuxer
    participant Wire as Serial Link (USART /dev/ttyS1)
    participant Mux as MCU HDLC Engine
    participant RPC as pw_rpc_avr::RpcServer
    participant Log as pw_log_avr
    participant App as Arduino Hardware / Pins

    Note over Host,App: Handshake inicial y sincronización de reloj (pw_chrono_avr)
    Host->>Wire: Frame HDLC [Addr 1: pw_rpc Ping(nonce)]
    Wire->>Mux: Frame 0x7E + CRC32
    Mux->>RPC: ProcessPacket(PingRequest)
    RPC-->>Mux: PacketType::kResponse (PingResponse)
    Mux-->>Wire: Frame HDLC [Addr 1: Response]
    Wire-->>Demux: Frame recibido OK
    Demux-->>Host: RPC completado (Latencia < 5 ms)

    Note over Host,App: Suscripción asíncrona a pines (Server Streaming)
    Host->>Wire: Frame HDLC [Addr 1: SubscribePins(pin=13, mode=CHANGE)]
    Wire->>Mux: Request recibido
    Mux->>RPC: Inicia stream RPC
    RPC-->>Host: Frame HDLC [Addr 1: PacketType::kServerStream (Pin 13 = HIGH)]
    
    Note over App,Wire: Evento de log interno en MCU
    App->>Log: PW_LOG_INFO("ADC sample ready: %d", val)
    Log->>Mux: Emite Token 32-bit (4 bytes) + Varint(val)
    Mux-->>Wire: Frame HDLC [Addr 2: Tokenized Log]
    Wire-->>Demux: Demux entrega a pw_tokenizer
    Demux->>Host: Decodifica string con archivo .elf y escribe a OpenWrt syslog
```

---

## 3. Matriz de Compatibilidad de Hardware Activa

| Parámetro | Arduino Mega 2560 | Arduino MKR WiFi 1010 | Arduino Nano ESP32 |
| :--- | :--- | :--- | :--- |
| **Microcontrolador** | ATmega2560 | ATSAMD21G18A | ESP32-S3 |
| **Arquitectura** | 8-bit AVR RISC | 32-bit ARM Cortex-M0+ | 32-bit Xtensa LX7 dual-core |
| **Frecuencia Reloj** | 16 MHz | 48 MHz | 240 MHz |
| **Memoria Flash** | 256 KB | 256 KB | 3.3 MB |
| **Memoria SRAM** | **8 KB (Crítico)** | 32 KB | 320 KB |
| **Toolchain C++** | `avr-gcc` (-std=c++17) | `arm-none-eabi-gcc` (-std=c++17) | `xtensa-esp32s3-elf-gcc` (-std=c++17) |
| **Soporte Pigweed** | `isantolin/avr_pigweed_port` | Upstream nativo de Google | Upstream nativo de Google |
| **Buffer HDLC Asignado** | 256 B | 512 B | 1024 B |
| **Buffer RPC Asignado** | 256 B | 512 B | 1024 B |
| **Consumo SRAM Stack** | ~1.15 KB (14.4%) | ~1.4 KB (4.3%) | ~2.5 KB (0.7%) |

---

## 4. Presupuesto Estricto de Recursos para ATmega2560

Para garantizar la estabilidad operacional y cumplir normas SIL-2:
1. **Zero Dynamic Allocation Policy:** Queda terminantemente prohibido el uso de `malloc`, `free`, `new`, `delete` o contenedores con crecimiento dinámico (`std::vector`). Todos los buffers se asignan estáticamente como `std::array`, `pw::span` o `pw::Vector<T, N>`.
2. **Strings y Aserciones en PROGMEM:** El backend `pw_assert_avr` utiliza `__builtin_trap()` y reportes de pánico con literales en memoria Flash (`PROGMEM`), con un coste de **0 bytes en SRAM**.
3. **Criptografía Optimizada:** `pw_crypto_avr` ubica las tablas constantes y S-boxes de SHA-256 en Flash, liberando ~512 bytes de SRAM frente a implementaciones estándar.

---

## 5. Hoja de Ruta de Migración en 5 Fases

```mermaid
flowchart TD
    P0[Fase 0: Estabilización de CI PR #56] --> P1[Fase 1: Submódulo Git y Dual-Stack HDLC]
    P1 --> P2[Fase 2: Integración de PigweedAVR en Firmware Arduino]
    P2 --> P3[Fase 3: Modernización del Daemon Python OpenWrt]
    P3 --> P4[Fase 4: Migración de Esquemas .proto a gRPC]
    P4 --> P5[Fase 5: Retiro Legacy, Auditoría SIL-2 y Validación]
```

### Fase 0: Estabilización Inmediata del PR #56
* **Objetivo:** Resolver el déficit de 0.1% en `pure branch coverage` en [`mcubridge/mcubridge/services/ubus.py`](file:///home/ignaciosantolin/arduino-yun-bridge2/mcubridge/mcubridge/services/ubus.py).
* **Entregable:** 25 de 25 checks de CI en verde y merge a `main`.

### Fase 1: Incorporación del Submódulo y Framing Dual-Stack
* **Estrategia Aprobada:** Vincular como Git Submodule:
  ```bash
  git submodule add https://github.com/isantolin/avr_pigweed_port.git third_party/avr_pigweed_port
  ```
* **Acciones:**
  1. Configurar un receptor dual-stack en el MCU capaz de aceptar tanto tramas COBS actuales (`0x00`) como tramas HDLC (`0x7E`).
  2. Implementar [`avr_port::ArduinoStream`](file:///home/ignaciosantolin/avr_pigweed_port/src/arduino_stream.h) para encapsular `Serial1` con buffers circulares por interrupción.

### Fase 2: Integración de `PigweedAVR` en `mcubridge-library-arduino`
* **Acciones:**
  1. Integrar el pipeline de exportación de biblioteca en el script de build.
  2. Activar `pw_rpc_avr::RpcServer` en el firmware base de Arduino.
  3. Vincular `pw_log_avr` para enviar los logs tokenizados hacia el Canal 2 de HDLC.
  4. Conectar `pw_chrono_avr` al reloj de milisegundos atómico.
* **Validación:** Emulación en `simavr` (Mega 2560), `Renode` (SAMD21) y `QEMU` (ESP32-S3).

### Fase 3: Modernización del Daemon OpenWrt (`mcubridge`)
* **Acciones:**
  1. Agregar dependencias oficiales (`pw_rpc`, `pw_hdlc`, `pw_tokenizer`) en [`pyproject.toml`](file:///home/ignaciosantolin/arduino-yun-bridge2/pyproject.toml).
  2. Reemplazar la máquina de decodificación serie por `pw_hdlc.Decoder` con demultiplexación de canales.
  3. Crear el servicio de detokenización de logs: el daemon carga el `.elf` del firmware compilado y vuelca los logs decodificados en `syslog` de OpenWrt.

### Fase 4: Migración de Esquemas Protobuf a Servicios gRPC
* **Acciones:**
  1. Sustituir los command IDs numéricos en [`tools/protocol/mcu_wire.proto`](file:///home/ignaciosantolin/arduino-yun-bridge2/tools/protocol/mcu_wire.proto) por interfaces estándar `service`:
     ```protobuf
     syntax = "proto3";
     package mcubridge.rpc;

     service GpioService {
       rpc DigitalWrite(DigitalWriteRequest) returns (DigitalWriteResponse);
       rpc DigitalRead(DigitalReadRequest) returns (DigitalReadResponse);
       rpc AnalogWrite(AnalogWriteRequest) returns (AnalogWriteResponse);
       rpc AnalogRead(AnalogReadRequest) returns (AnalogReadResponse);
       rpc SubscribePins(PinSubscriptionRequest) returns (stream PinNotification);
     }

     service BridgeControlService {
       rpc Ping(PingRequest) returns (PingResponse);
       rpc GetCapabilities(CapabilitiesRequest) returns (CapabilitiesResponse);
       rpc SynchronizeClock(ClockSyncRequest) returns (ClockSyncResponse);
     }
     ```
  2. Implementar el generador de stubs en `tools/protocol/generate.py`.

### Fase 5: Retiro de Código Legacy y Validación Final
* **Acciones:**
  1. Eliminar el parser COBS ad-hoc, el despachador switch manual y las colas de correlación antiguas.
  2. Ejecutar auditoría SIL-2, pruebas de estrés y validación en hardware físico real (Arduino Mega 2560 conectado a la VM OpenWrt interactiva).

---

## 6. Preservación Estricta de Contratos Superiores

Las interfaces públicas del sistema se mantienen inalteradas:
* **OpenWrt UBUS RPC:** `ubus call mcubridge status` y `ubus call mcubridge digital_write` no cambian su firma ni su salida JSON.
* **LuCI Web UI:** Las vistas web y llamadas XHR/CGI continúan funcionando sin modificación alguna.
* **Cloud Gateway:** El enlace gRPC mTLS hacia la nube permanece 100% aislado de los cambios en el bus serie.

---

## 7. Tabla Comparativa de Componentes a Sustituir

| Subsistema / Función | Componente Actual (`arduino-yun-bridge2`) | Componente Pigweed Sustituto | Líneas Eliminadas / Sustituidas | Beneficio y Justificación Técnica |
| :--- | :--- | :--- | :---: | :--- |
| **Delimitación y Framing Serie** | COBS ad-hoc (`PacketSerial`, `rpc_frame.h` en MCU y `frame.py` en Python) | `pw_hdlc` (`pw::hdlc::Encoder` / `Decoder` en C++ y `pw_hdlc.decode.FrameDecoder` en Python) | **~290 l** | Pasa de un único flujo delimitado por `0x00` a tramas HDLC (`0x7E` + CRC32) con **multiplexación nativa por canales lógicos** (Dirección 1=RPC, 2=Logs, 3=Consola). |
| **Control de Integridad / Checksum** | CRC-32 ad-hoc (en C++ `etl::crc32_t16` y `zlib.crc32` en Python) | `pw_checksum` (`pw::checksum::Crc32` con backend configurable `PW_CHECKSUM_CRC32_4BITS` o `1BITS`) | **~110 l** | Estandarizado, validado a nivel de bit, y con tabla reducida (64 B o 0 B) optimizada para los 8 KB de SRAM del ATmega2560. |
| **Sobre y Encabezado de Mensajes** | `RpcEnvelope` en `mcu_wire.proto` (con `command_id`, `correlation_id`, `payload`) + `rpc_structs.h` | `pw::rpc_avr::RpcPacket` (C++) y `pw.rpc.internal.Packet` (wire format oficial de Pigweed) | **~928 l** | Elimina la correlación manual de paquetes y los IDs numéricos arbitrarios; adopta el estándar binario gRPC de Google. |
| **Despacho de RPC en MCU** | `rpc_dispatch_table.inc` (`switch (envelope.command_id)`), `rpc_protocol.h` y `Bridge.cpp` | `pw::rpc_avr::RpcServer` y clases de servicio modulares derivadas de `pw::rpc_avr::RpcService` | **~513 l** | Elimina el switch gigante estático y permite desacoplar los servicios (`GpioService`, `BridgeService`, etc.) de forma modular y orientada a objetos. |
| **Definición de Protocolo e IDL** | Enums numéricos de `CommandId` en `mcu_wire.proto` + generador ad-hoc en `tools/protocol/generate.py` | Definición formal gRPC `service` en `.proto` + generación de stubs con `pw_rpc` / Nanopb | **~320 l** | Contratos de interfaz fuertemente tipados con generación automática de stubs para cliente y servidor. |
| **Suscripción de Pines / Eventos** | Inyección no solicitada de tramas de evento + desmultiplexación manual en Python (`gpio.py` y `runtime.py`) | **Server Streaming RPC** (`rpc SubscribePins(...) returns (stream PinNotification)`) | **~230 l** | Resuelve de raíz las notificaciones asíncronas con gestión formal del ciclo de vida del stream, backpressure y cierre determinista. |
| **Logging y Diagnóstico en MCU** | `DEBUG_PRINTLN` / strings literales en Flash (`F()`) o buffers de texto plano por serial | `pw_tokenizer` + `pw_log_avr` | **~120 l** | Los strings se hashean a tokens de 32 bits en compilación; por el cable viajan solo 4 bytes + varints. Ahorra **8–12 KB de Flash** en el ATmega2560 y reduce el tráfico serie un 80%. |
| **Adaptador Serie de Hardware** | Acceso directo a registros o `Stream&` mediante `BridgeClass::begin` / `hal.cpp` | `avr_port::ArduinoStream` y `avr_port::AvrUartStream` (USART0 y USART1) | **~85 l** | Implementa la interfaz estándar `pw::stream::NonSeekableReaderWriter` con buffers circulares protegidos por interrupción (evita overrun de hardware). |
| **Base de Tiempo y Temporización** | FSM manual de sincronización `ClockSyncService` en Python y `_handleClockSync` en C++ | `pw_chrono` + `pw_chrono_avr` (`millis()` bajo Arduino o Timer0 CTC atómico bajo bare-metal) | **~230 l** | Base de tiempo estándar de alta precisión compatible con `std::chrono` y segura contra concurrencia (`ATOMIC_BLOCK`). |
| **Aserciones y Fail-Stop (SIL-2)** | Macros `BRIDGE_ASSERT` propietarias llamando a `abort()` | `pw_assert` + `pw_assert_avr` con panic hook y diagnóstico de línea en `PROGMEM` | **~40 l** | Trazabilidad del fallo con emisión de la línea por UART sin consumir ni 1 solo byte de SRAM. |
| **Cliente de Comunicación en MPU** | `SerialTransport` en Python (manejo manual de timeouts, colas de correlación y reconexión) | `pw_hdlc.Decoder` + `pw_rpc.Client` (librerías oficiales de Pigweed para Python) | **~280 l** | Reemplaza cientos de líneas de lógica asíncrona propietaria por un cliente gRPC maduro y probado en producción. |
| **TOTAL ESTIMADO** | **Código propietario a retirar en C++, Python y Tooling** | **Ecosistema modular Pigweed estándar** | **~3,146 l** | **Reducción neta masiva de complejidad y superficie de ataque, ganando robustez SIL-2 y streaming bidireccional.** |


