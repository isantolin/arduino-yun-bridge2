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

## Estado de referencia en main

| Subsistema | Estado comprobado | Pendiente |
|---|---|---|
| HDLC y canales lógicos | Integrados en firmware y daemon; canales RPC, log y consola | Eliminar rutas manuales redundantes y probar los límites de cada canal |
| RPC MCU | Servicios Nanopb generados y registrados en firmware; `pw::rpc::Server` y `pw::rpc::Client` presentes | Simplificar registro, lifecycle y dispatch sin wrappers duplicados |
| Esquema Protobuf | Servicios `GpioService`, `SystemService`, `ClockSyncService`, `SpiService`, `FileSystemService`, `MailboxService`, `ProcessService` y `DataStoreService` declarados; `CMD_PIN_SUBSCRIBE_RESP` y `CMD_PIN_UPDATE_EVENT` eliminados del wire MCU | Limpiar metadatos/IDs legacy restantes tras migrar la API de aplicación |
| Descriptores Python | `PW_SERVICES` se construye con `pw_rpc.descriptors.Services`; `OperationSpec` deriva tipos e IDs; `BY_SERVER_STREAM` eliminado | Retirar empaquetado manual residual tras migrar los comandos restantes |
| Cliente RPC Python | `serial.py` invoca métodos unary (PR #68) y server-streaming (`GpioService.SubscribePins`) con `pw_rpc.Client` y descriptores oficiales; `PendingCommand` y `correlate_frame` eliminados (`send_raw` solo para handshake, baudrate, consola y `CMD_FILE_READ`) | Completado |
| GPIO streaming | Firmware expone `SubscribePins` con `NanopbServerWriter` y el daemon consume el stream nativo vía `pw_rpc.Client` con cancelación, cierre, reconexión y backpressure | Completado |
| API de aplicación | UBUS y servicios internos usan operaciones/Command IDs como compatibilidad de aplicación | Desacoplar la API de aplicación de los IDs de transporte |
| Generación | Buf/Nanopb y generador propio producen bindings y metadatos | Reducir generación manual a partir de descriptores y eliminar salidas/tablas redundantes |
| Transferencia de archivos | Servicio FileSystem RPC unary existente | Evaluar `pw_transfer` con límites, memoria y reanudación; no adoptarlo sin beneficio medido |
| Estructura C++ | Servicios Pigweed están en `Bridge.cpp`; HAL y servicios de dominio conservados | Separar responsabilidades y retirar glue manual innecesario |
| Contenedores | Estructuras fijas ETL/arrays/span usadas en MCU | Revisar cada buffer/copia y justificar el tamaño por arquitectura |
| CI y validación | PR #67 y PR #68 integrados en `main`; CI de estabilización aceptada por el mantenedor | Mantener matriz completa verde en cada cambio y verificar OpenWrt APK cuando corresponda |

## Orden de ejecución

| Orden | Etapa | Trabajo y criterio de salida | Estado |
|---:|---|---|---|
| 0 | Estabilización de CI | Matriz de CI verde en `main`; PR #67 integrado | **Completada** |
| 1 | Inventario y ADR | Contrastar implementación con ADR-0002; identificar rutas Pigweed reales y restos legacy. Este documento consolida el orden actualizado | **Completada como baseline; revisar durante cada etapa** |
| 2 | Cliente RPC Python | Sustituir construcción/decodificación manual de `RpcPacket`, IDs/call IDs y `PendingCommand` por `pw_rpc.Client`; mantener adaptadores mínimos para HDLC, AEAD y escritura serial asíncrona. Cubrir unary, error, timeout, cancelación, reconexión y concurrencia | **Completada (PR #68)** |
| 3 | Streaming GPIO | Consumir `GpioService.SubscribePins` como server-streaming Pigweed; eliminar polling/confirmación y demultiplexación manual solo cuando el ciclo de vida del stream esté cubierto. Probar cancelación, cierre, reconexión y backpressure | **En curso** |
| 4 | API de aplicación | Cambiar los servicios Python y clientes para invocar métodos RPC por contrato/descriptores, no por `CMD_*`; mantener UBUS y contratos públicos. Separar cualquier ID legado que siga siendo necesario solo en migración/diagnóstico | **Siguiente** |
| 5 | Generador | Derivar tipos, cardinalidad, service/method IDs, handlers y validaciones desde descriptors Protobuf/Pigweed; retirar catálogos duplicados y plantillas que repliquen metadatos. Regeneración reproducible y sin diff espurio | Pendiente |
| 6 | Firmware | Consolidar los servicios MCU generados y `pw::rpc::Server` y `pw::rpc::Client`; retirar adaptadores de paquetes y despacho manual que ya no aporten valor. Mantener HAL, servicios de hardware, AEAD y restricciones AVR | Pendiente |
| 7 | Transferencias | Evaluar `pw_transfer` frente a FileSystem unary RPC: tamaño máximo, streaming, RAM/Flash, throughput, cancelación, reanudación y seguridad. Adoptar solo si reduce complejidad y cumple límites | Pendiente |
| 8 | Estructura C++ | Dividir `Bridge.cpp` por responsabilidades (transporte, sesión/seguridad, servicios y coordinación) sin crear capas ceremoniales; usar interfaces Pigweed existentes | Pendiente |
| 9 | Contenedores y buffers | Revisar buffers HDLC/RPC/AEAD, ETL containers, copias y stack por arquitectura. Preferir `pw::span`, `etl::span`, arrays fijos y estructuras Pigweed/ETL adecuadas; no usar STL dinámica en AVR | Pendiente |
| 10 | Validación integral | Matriz Host, simavr, Renode y QEMU; pruebas de protocolo/fuzz/estrés; verificación de UBUS/LuCI/gateway; medir Flash, SRAM, stack, latencia y líneas manuales eliminadas. Documentar límites de hardware real | Pendiente |

## Detalle de la etapa 2: cliente RPC Python (PR #68)

El punto de partida está en `mcubridge/mcubridge/transport/serial.py`:

- Ya usa `pw_hdlc.decode.FrameDecoder` y decodifica `RpcPacket` con `pw_rpc.packets`.
- Todavía resuelve operaciones por `OperationSpec`, construye paquetes en `protocol.encode_rpc_packet/encode_hdlc_rpc`, mantiene un único `PendingCommand`, asigna `call_id` manualmente y correlaciona respuestas en `correlate_frame`.
- `PW_SERVICES` ya se genera desde descriptores oficiales. Se debe reutilizar esa metadata para configurar `pw_rpc.Client`, sin reconstruir una segunda tabla de métodos.
- La escritura serial/AEAD y el procesamiento de HDLC son límites de transporte válidos; no deben convertirse en una implementación paralela de RPC.

Criterios de aceptación:

1. Los métodos unary se invocan mediante el cliente oficial Pigweed y sus descriptores.
2. Las respuestas, errores y timeouts completan el Future correcto por llamada, permitiendo varias llamadas concurrentes sin colisión de `call_id`.
3. `process_packet` recibe los paquetes RPC entrantes después de validar HDLC y descifrar AEAD cuando corresponda.
4. El canal de salida Pigweed entrega bytes al writer serial sin bloquear el event loop y conserva el orden requerido por el transporte.
5. Handshake, LinkReset, cambio de baudrate y consola siguen funcionando, con excepciones explícitas si necesitan un flujo de control previo o fuera de RPC.
6. Las pruebas demuestran equivalencia funcional y no dependen de mocks que reimplementen internamente Pigweed.
7. El código manual retirado y el código nuevo se cuantifican; no se acepta una capa adicional que aumente la complejidad sin reemplazar lógica.

## Detalle de la etapa 3: streaming GPIO

El firmware (`mcubridge-library-arduino/src/Bridge.cpp`) ya expone `GpioService::SubscribePins` mediante `pw::rpc::NanopbServerWriter<rpc_pb_PinUpdateEvent>`: rechaza suscripciones inválidas o sin slots libres con `pw::Status::ResourceExhausted()`, emite eventos `PinUpdateEvent` como paquetes `PacketType::SERVER_STREAM` en `Bridge.sendPinEvent()`, y cierra el stream con `pw::OkStatus()` cuando no quedan pines suscritos.

Cambios en el daemon y el contrato:

1. `SerialTransport` invoca `rpc.pb.GpioService/SubscribePins` como método `SERVER_STREAMING` a través de `pw_rpc.Client`, registrando callbacks `on_next`, `on_completed` y `on_error`.
2. Los paquetes `PacketType.SERVER_STREAM`, `PacketType.RESPONSE` y `PacketType.SERVER_ERROR` asociados al `call_id` activo del stream se entregan a `rpc_client.process_packet`, encolando `PinUpdateEvent` en una cola acotada (`_stream_event_queue`) con política de backpressure (descarta el evento más antiguo si la cola se llena) antes de despachar a `GpioService.handle_pin_update_event`.
3. Se eliminan `CMD_PIN_SUBSCRIBE_RESP`, `CMD_PIN_UPDATE_EVENT`, `BY_SERVER_STREAM`, `GpioService.on_subscribe_resp`, `PendingCommand` y `SerialTransport.correlate_frame`.
4. `GpioService` cancela explícitamente el stream (`cancel_pin_stream`) al deshabilitar la última suscripción activa y restaura las suscripciones activas (`resubscribe_pins`) tras reconectar y sincronizar el enlace serie.

## Medición y reporte por etapa

Cada PR debe reportar:

- Archivos y rutas legacy eliminadas o reducidas.
- APIs Pigweed usadas directamente y justificación de cada adaptador restante.
- Número de líneas manuales antes/después, separando producción, tests y código generado.
- Resultados de tests y CI por arquitectura.
- Flash, SRAM estática, stack máximo y buffers para AVR/SAMD/ESP32 cuando cambien.
- Riesgos de despliegue y si se requiere actualizar firmware y daemon juntos.

No contabilizar código generado como reducción de código manual ni comparar líneas sin separar tests y scaffolding. No eliminar código únicamente por su nombre: verificar todos los productores, consumidores y rutas de seguridad antes de retirarlo.
