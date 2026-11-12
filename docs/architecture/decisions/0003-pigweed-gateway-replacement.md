# ADR-0003: Reemplazo integral del Gateway por Pigweed

- **Estado:** aprobado para implementación en PR #84.
- **Supersede:** cualquier decisión que indique alojar el nuevo endpoint dentro del proceso Python `mcubridge-gateway` existente.
- **Objetivo:** sustituir el Gateway actual por una implementación nueva basada directamente en Pigweed. No mantener dos Gateways, un modo de compatibilidad ni una ruta paralela.

## Decisión

El Gateway Python/grpclib actual se reemplazará por un único servicio Linux nativo C++ que use las implementaciones upstream de Pigweed:

- `pw_rpc::Server` como motor RPC.
- `pw_transfer::TransferService` como protocolo único para contenido binario y archivos.
- `pw_hdlc` como framing del transporte RPC.
- TLS mutuo en el transporte de red para autenticar clientes y nodos edge.
- Protobuf como fuente única de contratos; bindings generados por Pigweed.
- `pw::stream` y handlers de transferencia con acceso aleatorio y buffers acotados; nunca cargar archivos completos en memoria.

El nuevo Gateway será el único proceso/servicio central. No se añadirá un sidecar al Gateway anterior ni se conservará el servidor grpclib para fallback. La sustitución se hará como un corte coordinado: el paquete/servicio anterior y sus consumidores se eliminan cuando el reemplazo tenga paridad funcional y las pruebas de migración pasen. No se ejecutarán ambos sistemas en producción.

## Límites y responsabilidades

1. **Gateway Linux:** termina TLS, autentica la identidad del peer, crea el contexto RPC por conexión, aplica autorización por dispositivo y registra los servicios Pigweed. Las transferencias se asocian al principal autenticado, nunca a un identificador de dispositivo libre enviado por el cliente.
2. **MPU edge:** conserva la autoridad sobre su filesystem local. La nueva sesión Pigweed del edge ejecuta las operaciones de archivo solicitadas por el Gateway; el Gateway no simula ser propietario del disco remoto.
3. **MCU:** mantiene el enlace serial HDLC y el firmware upstream `pw_rpc`/`pw_transfer`. Los recursos MCU (por ejemplo EEPROM) se identifican por resource ID, no como rutas de filesystem.
4. **Clientes:** migran a clientes Pigweed y descriptores generados. No se mantiene un adaptador gRPC LocalBridge ni métodos unary de contenido de archivo.

## Transporte y sesiones

- El Gateway expone un único endpoint TLS para conexiones Pigweed; no se abre un listener adicional por transferencia.
- Cada conexión autenticada obtiene su propio contexto RPC y su propia tabla de canales/llamadas.
- La identidad del certificado TLS determina el principal y el dispositivo autorizado.
- La desconexión cancela llamadas y transferencias asociadas y libera handlers, streams y buffers.
- Los paquetes Pigweed se transportan sobre HDLC en el socket TLS. No se introduce un protocolo RPC propietario ni un segundo framing de aplicación.
- La interoperabilidad Python/C++ se valida con los clientes Pigweed upstream y paquetes de prueba compartidos.

## Migración sin legado

La sustitución es completa, no incremental en producción. Antes del corte se deben completar en la misma línea de implementación:

- Servicios de control, telemetría, consola, mailbox y ejecución remota necesarios para reemplazar las funciones actuales.
- Sesiones edge salientes autenticadas y despacho bidireccional.
- Transferencias de lectura y escritura con offset/resume, límites, cuotas, cancelación y limpieza transaccional.
- Autorización de operaciones derivada del certificado y del descriptor del servicio.
- Observabilidad equivalente y health/readiness del nuevo proceso.
- Migración de CLI, ejemplos, pruebas, empaquetado OpenWrt/host y despliegue.
- Eliminación de `grpclib`, `CloudBridge`, `LocalBridge`, `GatewayLocalBridgeService`, `FileRead`, `FileWrite` y sus adaptadores, una vez reemplazados por contratos Pigweed.
- Eliminación del servicio, Dockerfile, dependencias, scripts y documentación del Gateway anterior.

No se permite declarar la migración terminada mientras quede un consumidor de las APIs anteriores, una dependencia de grpclib en el Gateway nuevo o un fallback de transferencia unary.

## Criterios de aceptación

1. El binario nuevo se construye y ejecuta en Linux x86_64 y ARM64; se valida también el empaquetado OpenWrt que corresponda al despliegue real.
2. Un edge autenticado se conecta, publica telemetría, recibe comandos y desconecta/reconecta sin perder el aislamiento de sesión.
3. Clientes Python Pigweed y firmware C++ interoperan con el Gateway C++ usando descriptores upstream.
4. Lectura/escritura de archivos grandes se hace por chunks, con offsets, reanudación, límites y consumo de memoria acotado.
5. Pruebas negativas cubren certificados inválidos, identidad suplantada, acceso cruzado entre dispositivos, rutas traversal/symlink, cuota, cancelación y desconexión durante escritura.
6. La CI completa pasa y no hay imports, procesos, servicios, listeners, APIs ni tests del Gateway antiguo.
7. La sustitución de producción se realiza como un único corte; no se ofrece compatibilidad con el protocolo anterior.

## Restricciones de implementación

- Usar Pigweed upstream; no reimplementar `pw_rpc`, `pw_transfer`, HDLC, serialización o correlación de llamadas.
- No crear `pw_rpc_avr`, `pw_transfer_avr`, `pw_transfer_esp` ni forks por plataforma.
- No usar Python como servidor RPC Pigweed: los paquetes Python disponibles son clientes.
- No implementar un servidor C++ dentro del proceso grpclib antiguo. El nuevo Gateway es un reemplazo independiente.
- No borrar el Gateway anterior antes de tener el reemplazo validado y desplegable; sí eliminarlo en el mismo corte de release, sin convivencia ni fallback.
