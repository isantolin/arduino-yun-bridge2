# Plan de migración a Pigweed: ejecución consolidada

- **Estado:** ejecución aprobada; CI estabilizada en `main`.
- **Base:** ADR-0002 — `docs/architecture/decisions/0002-pigweed-hdlc-rpc-migration.md`.
- **Protocolo canónico:** `tools/protocol/mcu_wire.proto` y `tools/protocol/config_schema.proto`.
- **Integración MCU:** submódulo `third_party/avr_pigweed_port`, exportado como biblioteca Arduino `PigweedAVR`.
- **Alcance de hardware:** AVR Mega 2560, SAMD21 y ESP32-S3. No ampliar esta migración a Arduino Yún ni a su plataforma OpenWrt específica.

Este documento sustituye la hoja de ruta histórica de cinco fases, que describía como pendientes tareas ya integradas (HDLC, servicios Protobuf/Pigweed y servidor RPC del firmware). El estado se debe actualizar con evidencia de código, pruebas y CI, no por la mera existencia de archivos generados.

## Objetivo y reglas de ejecución

Completar la migración de la comunicación MCU-MPU a las APIs nativas de Pigweed y reducir al mínimo el código manual de transporte, serialización, despacho, correlación y streaming.

Reglas obligatorias:

1. **Una sola fuente de verdad:** los contratos y servicios viven en los `.proto`; los bindings y metadatos se generan. No mantener tablas paralelas manuales de servicios, métodos, tipos o dispatch.
2. **Usar Pigweed directamente:** preferir `pw_rpc.Client`, `pw_rpc.Server`, `pw_hdlc`, `pw_tokenizer`, Nanopb y los descriptores oficiales frente a codificadores, decodificadores, correladores o wrappers duplicados.
3. **No añadir abstracciones sin necesidad:** un adaptador solo se justifica para integrar límites reales (asyncio, pyserial, AEAD, UBUS o HAL), no para volver a implementar una API Pigweed.
4. **Compatibilidad de recursos:** AVR ATmega2560 con buffers y almacenamiento estáticos, Zero-STL y sin asignación dinámica. Medir Flash, SRAM y stack en vez de asumir que una abstracción es gratuita.
5. **Preservar contratos externos:** mantener las firmas y respuestas de UBUS, LuCI, LocalBridge gRPC y CloudBridge mTLS.
6. **Preservar seguridad:** mantener AEAD, HKDF/HMAC, autenticación del handshake, anti-replay, límites de payload y errores explícitos.
7. **Conservar el runtime actual:** asyncio permanece como runtime del daemon; no migrar a pw_async2 ni sustituir HAL/UBUS/procd.
8. **Cambios de protocolo coordinados:** no reservar campos Protobuf eliminados; actualizar el contrato y todos sus consumidores conjuntamente. No prometer compatibilidad de firmware mixto si el wire format no la ofrece.
9. **Validación por etapa:** pruebas unitarias, de contrato, emulación y CI antes de avanzar a la etapa siguiente. No fusionar una etapa con CI fallida.

## Estado consolidado del ecosistema

| Subsistema | Estado comprobado | Pendiente |
|---|---|---|
| HDLC y canales lógicos | Integrados en firmware y daemon; canales RPC, log y consola; decodificación nativa con `pw_hdlc.decode.FrameDecoder` | Ninguno; rutas manuales redundantes purgadas |
| RPC MCU | Servicios Nanopb generados y registrados en firmware; `pw::rpc::Server` y `pw::rpc::Client` integrados sin capas de compatibilidad intermedias | Ninguno |
| Esquema Protobuf | `tools/protocol/mcu_wire.proto` y `config_schema.proto` 100% migrados a RPC Pigweed nativo; `enum Command`, `CMD_*`, `cmd_opts` eliminados | Ninguno |
| Descriptores Python | `PW_SERVICES` se construye con `pw_rpc.descriptors.Services`; `OperationSpec` desacoplado de IDs legacy; `BY_COMMAND_ID` eliminado | Ninguno |
| Cliente RPC Python | `serial.py` invoca RPC unarios y streaming con `pw_rpc.Client`; correlation y call_id nativos de Pigweed | Ninguno |
| GPIO streaming | Firmware expone `SubscribePins` con `NanopbServerWriter` y el daemon consume el stream nativo vía `pw_rpc.Client` con backpressure | Ninguno |
| API de aplicación | Servicios internos y UBUS invocan métodos RPC por nombre de servicio y método Protobuf; sin capas de compatibilidad legacy | Ninguno |
| Generador | Generador `generate.py` limpio de catálogos y tablas de comandos obsoletas | Ninguno |
| Transferencia de archivos | `FileSystemService.Read` implementado como `SERVER_STREAMING` nativo Pigweed (`NanopbServerWriter` y `NanopbClientReader`), multi-chunk, cero hilos | Ninguno |
| Estructura C++ | 0 supresiones `NOLINT`, sin shims legacy, llamadas directas a APIs de Nanopb y Pigweed | Ninguno |
| Contenedores | Buffers y almacenamiento estático Zero-STL sin asignación dinámica en AVR | Ninguno |
| CI y validación | Matriz de pruebas completa verde; cobertura C++ >= 97.8% ramas y 99.8% líneas; cobertura Python 97.81% líneas y 96.86% ramas; 0 warnings linters/estáticos | Ninguno |

## Orden de ejecución

| Orden | Etapa | Trabajo y criterio de salida | Estado |
|---:|---|---|---|
| 0 | Estabilización de CI | Matriz de CI verde en `main`; PR #67 integrado | **Completada** |
| 1 | Inventario y ADR | Contrastar implementación con ADR-0002; identificar rutas Pigweed reales y restos legacy. Este documento consolida el orden actualizado | **Completada** |
| 2 | Cliente RPC Python | Sustituir construcción/decodificación manual de `RpcPacket`, IDs/call IDs y `PendingCommand` por `pw_rpc.Client`; mantener adaptadores mínimos para HDLC, AEAD y escritura serial asíncrona. Cubrir unary, error, timeout, cancelación, reconexión y concurrencia | **Completada (PR #68)** |
| 3 | Streaming GPIO | Consumir `GpioService.SubscribePins` como server-streaming Pigweed; eliminar polling/confirmación y demultiplexación manual solo cuando el ciclo de vida del stream esté cubierto. Probar cancelación, cierre, reconexión y backpressure | **Completada** |
| 4 | API de aplicación | Cambiar los servicios Python y clientes para invocar métodos RPC por contrato/descriptores, sin `CMD_*`; mantener UBUS y contratos externos sin capas de compatibilidad innecesarias | **Completada** |
| 5 | Generador | Derivar tipos, cardinalidad, service/method IDs, handlers y validaciones desde descriptors Protobuf/Pigweed; retirar catálogos duplicados y tablas `commands` | **Completada** |
| 6 | Firmware | Consolidar los servicios MCU generados y `pw::rpc::Server` y `pw::rpc::Client`; retirar adaptadores de paquetes y despacho manual de comandos legacy. Mantener HAL, AEAD y restricciones AVR | **Completada** |
| 7 | Transferencias | Opción 1 adoptada: `FileSystemService.Read` como `SERVER_STREAMING` multi-chunk nativo de Pigweed con `NanopbServerWriter` y `NanopbClientReader`, cero hilos para AVR bare-metal | **Completada** |
| 8 | Estructura C++ | Eliminación total de capas de compatibilidad o shims legacy; 0 supresiones `NOLINT` en todo el código C++ de producción y tests | **Completada** |
| 9 | Contenedores y buffers | Buffers y almacenamiento estático Zero-STL sin asignación dinámica en AVR; uso de `etl::span` y `pw::span` | **Completada** |
| 10 | Validación integral | Validación completa: tests C++ (99.8% líneas, 97.8% ramas), Clang-Tidy (0 warnings, 0 NOLINT), Cppcheck (0 issues), tests Python (899 passed, 97.81% líneas, 96.86% ramas), auditoría AST y Semgrep (0 issues) | **Completada** |

## Detalle de la etapa 2: cliente RPC Python (PR #68)

Implementación consolidada en `mcubridge/mcubridge/transport/serial.py`:

- Usa `pw_hdlc.decode.FrameDecoder` y decodifica `RpcPacket` con `pw_rpc.packets`.
- Sustituyó `PendingCommand`, la asignación manual de `call_id` y `correlate_frame` por `pw_rpc.Client.from_modules(callback_client.Impl(), ...)` alimentado por los descriptores oficiales de `PW_SERVICES`.
- Enruta la cardinalidad del método (`Method.Type.UNARY` vs `Method.Type.SERVER_STREAMING`) directamente desde `pw_rpc.descriptors.Method`.
- Preserva y consume el `call_id` entrante (`_active_server_call_ids`) al responder peticiones iniciadas por el MCU (`DataStore.get`, `FileSystem.read`, `Process.runAsync`, `Process.poll`).
- La escritura serial/AEAD y el procesamiento de HDLC permanecen como límites de transporte válidos sin reimplementar el motor RPC.

Criterios de aceptación:

1. Los métodos unary se invocan mediante el cliente oficial Pigweed y sus descriptores.
2. Las respuestas, errores y timeouts completan el Future correcto por llamada, permitiendo varias llamadas concurrentes sin colisión de `call_id`.
3. `process_packet` recibe los paquetes RPC entrantes después de validar HDLC y descifrar AEAD cuando corresponda.
4. El canal de salida Pigweed entrega bytes al writer serial sin bloquear el event loop y conserva el orden requerido por el transporte.
5. Handshake, LinkReset, cambio de baudrate y consola siguen funcionando, con excepciones explícitas si necesitan un flujo de control previo o fuera de RPC.
6. Las pruebas demuestran equivalencia funcional y no dependen de mocks que reimplementen internamente Pigweed.
7. El código manual retirado y el código nuevo se cuantifican; no se acepta una capa adicional que aumente la complejidad sin reemplazar lógica.

## Detalle de la etapa 3: streaming GPIO y lectura de archivos (`SERVER_STREAMING`)

El firmware (`mcubridge-library-arduino/src/Bridge.cpp`) ya expone `GpioService::SubscribePins` mediante `pw::rpc::NanopbServerWriter<rpc_pb_PinUpdateEvent>`: rechaza suscripciones inválidas o sin slots libres con `pw::Status::ResourceExhausted()`, emite eventos `PinUpdateEvent` como paquetes `PacketType::SERVER_STREAM` en `Bridge.sendPinEvent()`, y cierra el stream con `pw::OkStatus()` cuando no quedan pines suscritos. Asimismo, `FileSystemService::Read` (`rpc Read(rpc.pb.FileRead) returns (stream rpc.pb.FileReadResponse)`) utiliza `pw::rpc::NanopbServerWriter<rpc_pb_FileReadResponse>` y `pw::rpc::NanopbClientReader<rpc_pb_FileReadResponse>` para transferir archivos multi-chunk en ambas direcciones sin requerir `pw_thread` / `pw_transfer` en AVR bare-metal.

Cambios en el daemon y el contrato:

1. `SerialTransport` invoca `rpc.pb.GpioService/SubscribePins` y `rpc.pb.FileSystemService/Read` como métodos `SERVER_STREAMING` a través de `pw_rpc.Client`, registrando callbacks `on_next`, `on_completed` y `on_error`.
2. Los paquetes `PacketType.SERVER_STREAM`, `PacketType.RESPONSE` y `PacketType.SERVER_ERROR` asociados al `call_id` activo del stream se entregan a `rpc_client.process_packet`, encolando `PinUpdateEvent` en una cola acotada (`_stream_event_queue`) con política de backpressure (descarta el evento más antiguo si la cola se llena) antes de despachar a `GpioService.handle_pin_update_event`, o acumulando fragmentos `FileReadResponse` hasta recibir el paquete terminal `PacketType.RESPONSE`.
3. Se eliminan `CMD_PIN_SUBSCRIBE_RESP`, `CMD_PIN_UPDATE_EVENT`, `BY_SERVER_STREAM`, `GpioService.on_subscribe_resp`, `PendingCommand` y `SerialTransport.correlate_frame`.
4. `GpioService` cancela explícitamente el stream (`cancel_pin_stream`) al deshabilitar la última suscripción activa y restaura las suscripciones activas (`resubscribe_pins`) tras reconectar y sincronizar el enlace serie.

## Detalle de las etapas 4 y 5: API de aplicación, servicios y generador sin comandos legacy

1. **Eliminación integral de `CMD_*` y tablas redundantes:**
   - Se removió completamente `enum Command` y la opción `rpc_command` de `tools/protocol/mcu_wire.proto` y `tools/protocol/config_schema.proto`.
   - Se eliminó `BY_COMMAND_ID` y `command_id` en `OperationSpec`. Toda la resolución de métodos en Python y C++ se realiza de forma directa mediante descriptores canónicos de Pigweed (`PW_SERVICES`) y Nanopb.
   - En `tools/protocol/generate.py` y sus plantillas Jinja2 (`protocol.py.j2`, `protocol_client.py.j2`, `rpc_protocol.h.j2`), se eliminaron las tablas de comandos estáticas y la lógica de catálogo paralela.
   - Los servicios de aplicación (`BridgeService`, `ClockSyncService`, `GpioService`, `RuntimeService`, `UbusService`, `FileSystemService`, etc.) y sus clientes invocan métodos nativos de `pw_rpc` por nombre de servicio y método Protobuf.
   - Se preservaron íntegramente los contratos hacia el norte (UBUS OpenWrt, LuCI y gRPC `LocalBridge`).

## Detalle de las etapas 6 y 7: Firmware y streaming de archivos (Opción 1)

1. **Firmware C++ Nanopb y Pigweed RPC:**
   - Se eliminó el dispatch manual de comandos numéricos y sus estructuras intermedias en `Bridge.cpp`.
   - Registro y ciclo de vida de servicios mediante `pw::rpc::Server` y `pw::rpc::Client`.
2. **Transferencias de archivos (Opción 1 nativa Pigweed RPC):**
   - Implementado `FileSystemService.Read` como RPC `SERVER_STREAMING` nativo (`rpc Read(rpc.pb.FileRead) returns (stream rpc.pb.FileReadResponse)`).
   - En MCU (`mcubridge-library-arduino/src/Bridge.cpp` y `FileSystem.cpp`), el firmware emite fragmentos de archivo con `pw::rpc::NanopbServerWriter<rpc_pb_FileReadResponse>` y recibe archivos mediante `pw::rpc::NanopbClientReader<rpc_pb_FileReadResponse>`.
   - Cero dependencias de `pw_thread` o memoria dinámica, garantizando compatibilidad total con AVR ATmega2560 bare-metal (Zero-STL, buffers estáticos).
   - Se eliminaron shims y artificios legacy (`FileReadResponse(content=b"")`).

## Detalle de las etapas 8 y 9: Estructura C++, cero supresiones NOLINT y gestión estática de memoria

1. **Calidad de código C++ y cero NOLINT:**
   - Cero supresiones `NOLINT` en todo el código C++ de producción y pruebas (`mcubridge-library-arduino/`).
   - Todos los checks de `clang-tidy` (bugprone, cert, clang-analyzer, cppcoreguidelines, misc, modernize, performance, readability) pasan con 0 advertencias.
   - `cppcheck` pasa con 0 errores y 0 advertencias sobre los 11 archivos C++ del proyecto.
2. **Contenedores y buffers estáticos:**
   - Buffers acotados fijos para HDLC, RPC y AEAD.
   - Cero asignación dinámica de memoria (`malloc`/`new`) en rutas críticas del MCU.
   - Uso de `etl::span` y `pw::span` para vistas de memoria seguras.

## Detalle de la etapa 10: Validación integral y métricas finales

1. **Evidencia de pruebas y análisis estático:**
   - **Pruebas y cobertura C++ (`tools/ci/coverage_arduino.sh`):**
     - Líneas: 99.8%
     - Funciones: 100.0%
     - Ramas: 97.8% (supera el umbral mandatorio $\ge 95\%$)
     - Clang-Tidy: 0 advertencias, 0 supresiones `NOLINT`.
     - Cppcheck: 0 advertencias, 0 errores.
   - **Pruebas y cobertura Python (`tools/ci/coverage_python.py`):**
     - 899 pruebas unitarias y de integración superadas (899 passed, 0 failed en 134s).
     - Cobertura total de líneas: 97.81% (supera $\ge 95\%$).
     - Cobertura pura de ramas: 96.86% (1017/1050, supera $\ge 95\%$).
   - **Herramientas de auditoría y tipado:**
     - Pyright: 0 errores, 0 advertencias.
     - Pyrefly: 0 diagnósticos.
     - Vulture: 0 código muerto detectado (código de salida 0).
     - Flake8: 0 problemas.
     - Black: 100% formateado según PEP 8.
     - `codebase_auditor.py` (Semgrep + AST): 0 violaciones detectadas.

## Medición y reporte por etapa

Cada PR debe reportar:

- Archivos y rutas legacy eliminadas o reducidas.
- APIs Pigweed usadas directamente y justificación de cada adaptador restante.
- Número de líneas manuales antes/después, separando producción, tests y código generado.
- Resultados de tests y CI por arquitectura.
- Flash, SRAM estática, stack máximo y buffers para AVR/SAMD/ESP32 cuando cambien.
- Riesgos de despliegue y si se requiere actualizar firmware y daemon juntos.

No contabilizar código generado como reducción de código manual ni comparar líneas sin separar tests y scaffolding. No eliminar código únicamente por su nombre: verificar todos los productores, consumidores y rutas de seguridad antes de retirarlo.
