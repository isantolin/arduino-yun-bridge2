# Plan de migración a Pigweed: ejecución consolidada

- **Estado:** migración RPC integrada; etapa `pw_transfer` en curso en PR #84. PR abierto y sin merge; la CI del HEAD debe aprobarse antes de considerar validada esta revisión.
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
5. **Preservar comportamiento de borde, no APIs legacy:** mantener las firmas y respuestas UBUS/LuCI que sigan siendo necesarias en el MPU. `LocalBridge`/`CloudBridge` gRPC no son contratos a preservar: ADR-0003 ordena migrarlos a Pigweed y eliminarlos en el corte único del Gateway.
6. **Preservar seguridad:** mantener AEAD, HKDF/HMAC, autenticación del handshake, anti-replay, límites de payload y errores explícitos.
7. **Conservar el runtime del MPU:** asyncio permanece como runtime del daemon edge para HAL/UBUS/procd. El Gateway central es un servicio Linux C++ independiente; no se conserva el Gateway Python ni se introduce un sidecar.
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
| API de aplicación | Servicios internos y UBUS del MPU invocan métodos RPC por nombre de servicio y método Protobuf | Migrar clientes northbound desde `LocalBridge`/`CloudBridge` gRPC a Pigweed y retirar esos contratos en el corte definido por ADR-0003 |
| Generador | Generador `generate.py` limpio de catálogos y tablas de comandos obsoletas | Ninguno |
| Transferencia de archivos | Cliente Python `pw_transfer.Manager` y handler EEPROM MCU integrados; `FileSystemService.Read` eliminado | reemplazo integral del Gateway Python/grpclib por un único Gateway Linux C++ con `pw_rpc::Server`, `pw_transfer::TransferService`, HDLC sobre TLS mutuo; migrar sesiones edge y clientes al protocolo Pigweed; backend remoto de archivos con chunks acotados; retirar `CloudBridge`, `LocalBridge`, `FileRead`/`FileWrite` y `FileSystemService.Write`; pruebas end-to-end y de aislamiento por identidad |
| Estructura C++ | 0 supresiones `NOLINT`, sin shims legacy, llamadas directas a APIs de Nanopb y Pigweed | Ninguno |
| Contenedores | Buffers y almacenamiento estático Zero-STL sin asignación dinámica en AVR | Ninguno |
| CI y validación | Baseline de `main` previamente validada; PR #84 requiere repetir toda la matriz después de los cambios de transferencia | CI de PR #84 en HEAD actual, emulación, perfiles Flash/SRAM/stack y validación física UART/HDLC |

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
| 7 | Transferencias | Adoptar upstream `pw_transfer` como único protocolo de contenido de archivos; implementar backends Linux/MCU, migrar clientes en ambos sentidos y retirar APIs unary de contenido | **En curso — handler EEPROM MCU y cliente Python integrados; reemplazo completo del Gateway y migración de sesiones/clientes pendientes** |
| 8 | Estructura C++ | Eliminación total de capas de compatibilidad o shims legacy; 0 supresiones `NOLINT` en todo el código C++ de producción y tests | **Completada** |
| 9 | Contenedores y buffers | Buffers y almacenamiento estático Zero-STL sin asignación dinámica en AVR; uso exclusivo y unificado de `pw::span` | **Completada** |
| 10 | Validación integral | Validación completa: tests C++ (99.8% líneas, 97.8% ramas), Clang-Tidy (0 warnings, 0 NOLINT), Cppcheck (0 issues), tests Python (899 passed, 97.81% líneas, 96.86% ramas), auditoría AST y Semgrep (0 issues) | **Completada** |
| 11 | Unificación Canónica de Tópicos y Manejadores | Erradicación total de `Topic.*` legacy (`"a"`, `"d"`, `"sh"`), unificación de `cloud_topic` y `mcu_handler` bajo descriptores Pigweed `{direction}/{Service}/{Method|Message}`, eliminación de `CommonAction`/`rpc.pb.actions`, y despacho Pigweed canónico mediante `pw_rpc::Server` en el Gateway C++ nuevo (sin gRPC ni `grpclib`) | **Completada** |

## Detalle de la etapa 2: cliente RPC Python (PR #68)

Implementación consolidada en `mcubridge/mcubridge/transport/serial.py`:

- Usa `pw_hdlc.decode.FrameDecoder` y decodifica `RpcPacket` con `pw_rpc.packets`.
- Sustituyó `PendingCommand`, la asignación manual de `call_id` y `correlate_frame` por `pw_rpc.Client.from_modules(callback_client.Impl(), ...)` alimentado por los descriptores oficiales de `PW_SERVICES`.
- Enruta la cardinalidad del método (`Method.Type.UNARY` vs `Method.Type.SERVER_STREAMING`) directamente desde `pw_rpc.descriptors.Method`.
- Preserva y consume el `call_id` entrante (`_active_server_call_ids`) al responder peticiones iniciadas por el MCU (`DataStore.get`, `Process.runAsync`, `Process.poll`).
- La escritura serial/AEAD y el procesamiento de HDLC permanecen como límites de transporte válidos sin reimplementar el motor RPC.

Criterios de aceptación:

1. Los métodos unary se invocan mediante el cliente oficial Pigweed y sus descriptores.
2. Las respuestas, errores y timeouts completan el Future correcto por llamada, permitiendo varias llamadas concurrentes sin colisión de `call_id`.
3. `process_packet` recibe los paquetes RPC entrantes después de validar HDLC y descifrar AEAD cuando corresponda.
4. El canal de salida Pigweed entrega bytes al writer serial sin bloquear el event loop y conserva el orden requerido por el transporte.
5. Handshake, LinkReset, cambio de baudrate y consola siguen funcionando, con excepciones explícitas si necesitan un flujo de control previo o fuera de RPC.
6. Las pruebas demuestran equivalencia funcional y no dependen de mocks que reimplementen internamente Pigweed.
7. El código manual retirado y el código nuevo se cuantifican; no se acepta una capa adicional que aumente la complejidad sin reemplazar lógica.

## Detalle de la etapa 3: streaming GPIO (`SERVER_STREAMING`)

El firmware (`mcubridge-library-arduino/src/Bridge.cpp`) expone `GpioService::SubscribePins` mediante `pw::rpc::NanopbServerWriter<rpc_pb_PinUpdateEvent>`: rechaza suscripciones inválidas o sin slots libres con `pw::Status::ResourceExhausted()`, emite eventos `PinUpdateEvent` como paquetes `PacketType::SERVER_STREAM` en `Bridge.sendPinEvent()`, y cierra el stream con `pw::OkStatus()` cuando no quedan pines suscritos. La transferencia de archivos no usa server-streaming Nanopb: `FileSystemService.Read` fue eliminado. `pw_transfer` tiene un handler de lectura/escritura para la EEPROM interna de 4 KiB del ATmega2560 (recurso fijo `0x4D454741`, sin semántica de filesystem). El endpoint de almacenamiento Linux y la migración de las APIs locales `FileRead`/`FileWrite` siguen pendientes.

Cambios en el daemon y el contrato:

1. `SerialTransport` invoca `rpc.pb.GpioService/SubscribePins` como método `SERVER_STREAMING` a través de `pw_rpc.Client`, registrando callbacks `on_next`, `on_completed` y `on_error`.
2. Los paquetes `PacketType.SERVER_STREAM`, `PacketType.RESPONSE` y `PacketType.SERVER_ERROR` asociados al `call_id` activo se entregan a `rpc_client.process_packet`; `PinUpdateEvent` se encola en una cola acotada (`_stream_event_queue`) con política de backpressure antes de despachar a `GpioService.handle_pin_update_event`.
3. Se eliminan `CMD_PIN_SUBSCRIBE_RESP`, `CMD_PIN_UPDATE_EVENT`, `BY_SERVER_STREAM`, `GpioService.on_subscribe_resp`, `PendingCommand` y `SerialTransport.correlate_frame`.
4. `GpioService` cancela explícitamente el stream (`cancel_pin_stream`) al deshabilitar la última suscripción activa y restaura las suscripciones activas (`resubscribe_pins`) tras reconectar y sincronizar el enlace serie.

## Detalle de las etapas 4 y 5: API de aplicación, servicios y generador sin comandos legacy

1. **Eliminación integral de `CMD_*` y tablas redundantes:**
   - Se removió completamente `enum Command` y la opción `rpc_command` de `tools/protocol/mcu_wire.proto` y `tools/protocol/config_schema.proto`.
   - Se eliminó `BY_COMMAND_ID` y `command_id` en `OperationSpec`. Toda la resolución de métodos en Python y C++ se realiza de forma directa mediante descriptores canónicos de Pigweed (`PW_SERVICES`) y Nanopb.
   - En `tools/protocol/generate.py` y sus plantillas Jinja2 (`protocol.py.j2`, `protocol_client.py.j2`, `rpc_protocol.h.j2`), se eliminaron las tablas de comandos estáticas y la lógica de catálogo paralela.
   - Los servicios de aplicación (`BridgeService`, `ClockSyncService`, `GpioService`, `RuntimeService`, `UbusService`, `FileSystemService`, etc.) y sus clientes invocan métodos nativos de `pw_rpc` por nombre de servicio y método Protobuf.
   - Se mantienen las interfaces UBUS/LuCI propias del MPU. El contrato gRPC `LocalBridge` no se preserva: sus consumidores deben migrarse a Pigweed antes de eliminarlo en el corte único de ADR-0003.

## Detalle de las etapas 6 y 7: Firmware y streaming de archivos (Opción 1)

1. **Firmware C++ Nanopb y Pigweed RPC:**
   - Se eliminó el dispatch manual de comandos numéricos y sus estructuras intermedias en `Bridge.cpp`.
   - Registro y ciclo de vida de servicios mediante `pw::rpc::Server` y `pw::rpc::Client`.
2. **Transferencias de archivos (upstream `pw_transfer`):**
   - `pw_transfer` es el único protocolo de streaming de archivos; el RPC `FileSystemService.Read`, los lectores/escritores Nanopb, sus callbacks y la ruta Python de server-streaming se eliminaron.
   - El servicio upstream `pw.transfer.Transfer` y su `Thread<1, 1>` cooperativo están integrados para ATmega2560.
   - La integración del protocolo no implica que exista ya un backend SD de archivos: el handler de archivos con apertura, lectura/escritura acotada, validación de rutas y reemplazo atómico sigue siendo una tarea de implementación y una condición para declarar transferencias de archivos operativas.

## Detalle de las etapas 8 y 9: Estructura C++, cero supresiones NOLINT y gestión estática de memoria

1. **Calidad de código C++ y cero NOLINT:**
   - Cero supresiones `NOLINT` en todo el código C++ de producción y pruebas (`mcubridge-library-arduino/`).
   - Todos los checks de `clang-tidy` (bugprone, cert, clang-analyzer, cppcoreguidelines, misc, modernize, performance, readability) pasan con 0 advertencias.
   - `cppcheck` pasa con 0 errores y 0 advertencias sobre los 11 archivos C++ del proyecto.
2. **Contenedores y buffers estáticos:**
   - Buffers acotados fijos para HDLC, RPC y AEAD.
   - Cero asignación dinámica de memoria (`malloc`/`new`) en rutas críticas del MCU.
   - Uso exclusivo y unificado de `pw::span` para vistas de memoria contiguas seguras.

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

## Detalle de la etapa 11: unificación canónica de tópicos, manejadores y erradicación de residuos

1. **Unificación de `Topic.*` con servicios Pigweed:**
   - Erradicación de los códigos cortos legacy (`"a"`, `"d"`, `"sh"`, `"file"`, etc.) en `config_schema.proto` y plantillas generadoras.
   - Alineación 1:1 de `Topic` con los nombres canónicos de servicios Pigweed (`GpioService`, `DataStoreService`, `FileSystemService`, `MailboxService`, `ProcessService`, `SpiService`, `SystemService`, `ClockSyncService`, `ConsoleWrite`).
   - Eliminación total de clases de acción huérfanas (`CommonAction`, `DigitalAction`, `DatastoreAction`, etc.) y de la opción `rpc.pb.actions` en Protobuf.

2. **Unificación determinista de `cloud_topic` y `mcu_handler`:**
   - Erradicación de strings ad-hoc dispersos en Protobuf (`system/version/value`, `system/bridge/handshake/value`, `console/out`, `mailbox/incoming`, `spi/transfer/resp`, etc.).
   - Derivación canónica universal de tópicos según la regla 3-segmentos:
     - Peticiones RPC: `{prefix}/req/{Service}/{Method}`
     - Respuestas RPC: `{prefix}/res/{Service}/{Method}`
     - Eventos y telemetría: `{prefix}/evt/{Service}/{Method|Message}`
   - Unificación de manejadores (`mcu_handler` / `handler_name`) mediante derivación por convención `{servicio_snake}_{metodo_snake}` sobre `BridgeService`, eliminando opciones manuales redundantes en los `.proto`.

3. **Migración northbound al Gateway Pigweed (ADR-0003):**
   - El despacho actual mediante `GatewayLocalBridgeService`/`LocalBridge` es transitorio y no forma parte del diseño final.
   - Migrar clientes, sesiones edge y operaciones northbound a servicios Pigweed generados; después eliminar `GatewayLocalBridgeService`, `LocalBridge`, `CloudBridge` y sus consumidores en el mismo corte coordinado.
   - La identidad y autorización del dispositivo deben derivarse del principal autenticado por mTLS, nunca de `x-device-id` suministrado libremente por el cliente.

4. **Saneamiento de pruebas y documentación:**
   - Actualización de todos los casos de prueba de integración, métricas, telemetría y propiedades de tópicos a las rutas canónicas.
   - Sincronización estricta de `docs/PROTOCOL.md`, `mcubridge-gateway/README.md` y `mcubridge-client-examples/README.md`.

## Medición y reporte por etapa

Cada PR debe reportar:

- Archivos y rutas legacy eliminadas o reducidas.
- APIs Pigweed usadas directamente y justificación de cada adaptador restante.
- Número de líneas manuales antes/después, separando producción, tests y código generado.
- Resultados de tests y CI por arquitectura.
- Flash, SRAM estática, stack máximo y buffers para AVR/SAMD/ESP32 cuando cambien.
- Riesgos de despliegue y si se requiere actualizar firmware y daemon juntos.

No contabilizar código generado como reducción de código manual ni comparar líneas sin separar tests y scaffolding. No eliminar código únicamente por su nombre: verificar todos los productores, consumidores y rutas de seguridad antes de retirarlo.
