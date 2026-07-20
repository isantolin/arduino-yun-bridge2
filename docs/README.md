# Arduino MCU Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![OpenWrt](https://img.shields.io/badge/OpenWrt-25.12.5-00B5E2?logo=openwrt)](https://openwrt.org/releases/25.12.5)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3130/)
[![C++ Standard](https://img.shields.io/badge/C%2B%2B-17-00599C?logo=cplusplus)](https://isocpp.org/)
[![ETL](https://img.shields.io/badge/ETL-SIL--2%20Compliant-green)](https://www.etlcpp.com/)
[![FIPS 140-3](https://img.shields.io/badge/Security-FIPS%20140--3-critical)](https://csrc.nist.gov/publications/detail/fips/140/3/final)

**MCU Bridge 2 es un reemplazo moderno, robusto y agnóstico de hardware para el sistema Bridge original.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en dispositivos OpenWrt, reemplazando la antigua solución basada en `python-bridge` por un daemon asíncrono y un protocolo RPC binario eficiente.

## Características Principales

- **Límites configurables:** Los buffers internos de consola y mailbox se pueden ajustar vía UCI (`console_queue_limit_bytes`, `mailbox_queue_limit`, `mailbox_queue_bytes_limit`) para prevenir desbordes. Se incluyen límites estrictos como `pending_pin_request_limit`, `file_write_max_bytes` y `file_storage_quota_bytes`.
- **Control de Flujo (Backpressure) en la Nube:** Regulación mediante colas de telemetría y buffer backpressure en el stream gRPC.
- **Respuestas correladas sobre gRPC:** El flujo bidireccional gRPC correlaciona de forma nativa peticiones y respuestas mediante identificadores estables (`sequence_id`).
- **Seguridad Funcional (SIL-2):** Librería MCU escrita en C++17 sin STL y sin alocación dinámica, garantizando determinismo y estabilidad.
- **MIL-SPEC Compliance (FIPS 140-3):** Implementación de **HKDF-SHA256** para derivación de claves y **Power-On Self-Tests (POST)** que validan el motor criptográfico en cada arranque.
- **Protección de Flash:** Bloqueo de inicio si las rutas de escritura intensa (`file_system_root`, `cloud_spool_dir`) no están en `/tmp` (RAM).

### Novedades (julio 2026)

- **Migración Integral a gRPC para Enlace con la Nube (v2.8.5)**: Reemplazo del protocolo de red TCP/TLS crudo en la conexión MPU-Nube por una arquitectura moderna y eficiente de **gRPC sobre HTTP/3 (QUIC) asíncrono y bidireccional (streaming) con soporte de fallback HTTP/2**. El daemon y el Cloud Gateway ahora se comunican de forma determinista mediante stubs de servicio tipados generados a partir de `mcubridge.proto` usando `grpclib`.
- **Exclusión de Archivos Autogenerados**: Los archivos autogenerados de gRPC y Protobuf (`*_grpc.py` y `*_pb2.py`) han sido excluidos formalmente de las validaciones estáticas de tipos y linters en `pyproject.toml`, e incorporados al archivo `.gitignore`.
- **Migración a Sockets UNIX (v2.8.5)**: Adopción de una arquitectura de IPC local de alto rendimiento basada en Sockets UNIX (`/var/run/mcubridge.sock`) y tramas binarias Protobuf prefijadas por longitud en el Linux MPU. Esto reduce las dependencias locales y elimina la necesidad de intermediarios de mensajería locales.
- **Exclusión de Directorios Temporales**: Optimización del linter (`black`/`ruff`) excluyendo `.tmp_tests` para evitar bloqueos e inconsistencias durante compilaciones concurrentes y ejecuciones de tests E2E.

### Novedades (junio 2026)

- **Migración Integral a Protobuf (v2.8.5)**: Las estructuras de telemetría, métricas y políticas de seguridad han sido migradas íntegramente de `msgspec` a **Protobuf + Nanopb**. Esto establece una Fuente Única de Verdad (SSOT) en `mcubridge.proto`, garantizando una consistencia binaria absoluta y reduciendo el overhead de procesamiento en el MPU.
- **Optimización de Despacho (Switch-based)**: El despacho de comandos en el MCU se ha refactorizado a una estructura `switch` optimizada sobre punteros a métodos, reduciendo el consumo de RAM respecto a las tablas de salto estáticas previas.
- **De-bloating de Flash (MCU)**: Consolidación de instanciaciones de Nanopb en archivos `.cpp` para evitar la duplicación de símbolos en el firmware, maximizando el espacio libre en MCUs con recursos limitados.

### Novedades (marzo 2026)

- **Unificación de Componentes (Shell + Process):** Eliminación de la capa redundante `ShellComponent`. Toda la lógica de comandos shell/consola vía gRPC/IPC ha sido absorbida por el **ProcessComponent**, centralizando la gestión de PIDs, concurrencia y políticas de seguridad en un solo módulo determinista.
- **Serialización protobuf/nanopb:** Todos los payloads RPC se definen en `mcubridge.proto` y se serializan como **protobuf**: clases `Packet` generadas en Python y structs `rpc::payload::*` sobre nanopb en C++.
- **Soporte PWM (Analog Write):** Implementación completa de `analog_write()` en el cliente Python, permitiendo el control de actuadores y regulación de potencia vía sockets locales e IPC.
- **Validación E2E Analógica:** Los tests de integración ahora cubren lecturas y escrituras analógicas de forma nativa.

### Novedades (febrero 2026)

- **Despacho O(1) en MCU:** Implementación de tablas de salto para comandos de sistema, GPIO, Mailbox y Process, optimizando el rendimiento y cumpliendo SIL-2.
- **Sincronía de Capacidades 100%:** Adición del bit de **SPI** y estandarización del bit de **Big Buffer** en todo el stack.
- **Observabilidad Refinada:** Logs hexadecimales estructurados `[DE AD BE EF]` con etiquetas direccionales inequívocas `[MCU -> SERIAL]` y `[SERIAL -> MCU]`.
- **Race Condition Protection:** Handshake robusto que maneja respuestas asíncronas de alta velocidad en emuladores.

### Novedades (enero 2026)

- **Cryptographic Self-Tests (KAT):** El sistema ahora realiza pruebas de respuesta conocida (Known Answer Tests) para SHA256 y HMAC-SHA256 al iniciar. Si las pruebas fallan, el sistema aborta el arranque (**Fail-Secure**).
- **Derivación de Claves HKDF (RFC 5869):** El handshake serie ya no usa el secreto compartido directamente; utiliza HKDF-SHA256 para derivar claves de autenticación efímeras, mejorando el aislamiento de claves.
- **Refactorización SIL-2 (C++):** Unificación de constructores delegados y validación defensiva de rangos en GPIO para prevenir accesos a memoria fuera de límites.
- **Compatibilidad Python 3.13:** Soporte completo para Python 3.13.9-r2 y uso de `asyncio.TaskGroup` para una gestión de tareas más robusta.

### Novedades (OpenWrt 25.12)

- **Detección de Hardware (Capabilities Discovery):** El protocolo incluye introspección (`CMD_GET_CAPABILITIES`) para conocer las características físicas del MCU (pines, arquitectura, features como EEPROM, DAC, FPU, I2C).
- **Integración con APK:** Paquetización moderna utilizando el sistema de paquetes **APK** de OpenWrt 25.12.
- **Transporte de Alto Rendimiento:** Uso de `python3-serialx` para un stack serie nativo sync/async con control directo de modem pins y transporte tipado.
- **10/10 Eficiencia (uvloop):** Activación de `uvloop` (implementación de alto rendimiento basada en `libuv`) como bucle de eventos, ofreciendo un throughput 2-4x superior en operaciones seriales e IPC intensivas.
- **Logging Hexadecimal:** Todo el tráfico binario se registra mediante volcados hexadecimales en syslog.
- **Manejo de Excepciones SIL-2:** Refactorización profunda del manejo de errores para eliminar capturas genéricas.

---

### Detalles Técnicos del Stack gRPC & Sockets UNIX (v2.8.5)

- **Cloud Gateway en gRPC:** La comunicación externa se realiza vía gRPC bidireccional sobre HTTP/3 (QUIC) con soporte para fallback HTTP/2, ofreciendo un canal seguro, tipado y de baja latencia.
- **Local IPC por Socket UNIX:** El daemon escucha localmente en `/var/run/mcubridge.sock`. Todos los clientes locales y CGI interaccionan mediante este socket, previniendo loops y reduciendo el overhead.
- **Datastore sin ida y vuelta al MCU:** Las consultas de datos locales se resuelven de forma inmediata en Linux usando la caché en memoria sincronizada por el daemon.
- **Telemetría consolidada:** `RuntimeState` registra métricas de drop/truncamiento por canal (`console_dropped_chunks`, `mailbox_truncated_messages`, etc.) y las expone en `/tmp/mcubridge_status.json`.
- **Persistencia vs Flash-wear:** Las colas de desbordamiento temporales apuntan a `/tmp` (`cloud_spool_dir=/tmp/mcubridge/spool`), mientras que las de almacenamiento estático pueden configurarse en directorios persistentes seguros (`file_system_root=/tmp/yun_files`).
- **Autodiagnóstico del Spool:** Si el spool en disco sufre de problemas de espacio o corrupción, el daemon degrada su estado a *best effort* de forma automática, registrando el incidente en la telemetría sin bloquear el sistema.
- **Falla en Seguro con TLS:** Si `cloud_tls=1` y el archivo `cloud_cafile` no existe en la ruta configurada, el daemon abortará el arranque inmediatamente.
- **Prometheus Integrado:** El exportador HTTP integrado expone métricas en el puerto 9130 para auditoría activa.

## Guía rápida de UCI
```sh
uci set mcubridge.general.cloud_host='127.0.0.1'
uci set mcubridge.general.cloud_port='8443'
uci set mcubridge.general.cloud_tls='1'
# Opcional: cafile (si está vacío se usa el trust store del sistema)
uci set mcubridge.general.cloud_cafile='/etc/ssl/certs/bridge-ca.pem'
# Opcional (mTLS): solo si tu gateway exige certificados de cliente
uci set mcubridge.general.cloud_certfile='/etc/mcubridge/client.crt'
uci set mcubridge.general.cloud_keyfile='/etc/mcubridge/client.key'
uci set mcubridge.general.allowed_commands='ls cat uptime'
uci set mcubridge.general.serial_retry_timeout='0.75'
uci set mcubridge.general.serial_retry_attempts='3'
uci set mcubridge.general.serial_response_timeout='3.0'
uci set mcubridge.general.serial_handshake_min_interval='0.0'
uci set mcubridge.general.serial_handshake_fatal_failures='3'
uci commit mcubridge
```
- Usa `allowed_commands='*'` solo en entornos controlados; cualquier otro valor se normaliza a minúsculas y se interpreta como lista explícita.
- Las rutas de certificados deben existir; de lo contrario, el daemon abortará el arranque.

## Plan de compatibilidad y toolchain

| Capa | Estado actual | Próximo paso controlado | Cómo se valida |
| --- | --- | --- | --- |
| Python (daemon en el MPU) | Base en Python 3.13.9-r2. | Mantener compatibilidad con futuras versiones. | `tox -e py313` |
| Toolchain OpenWrt | SDK 25.12.5 (APK). | Compilación de paquetes APK. | `./1_compile.sh` |
| MCU Firmware | C++17 / ETL (SIL-2). | Cobertura extrema sin STL. | `./tools/coverage_arduino.sh` |

- Para personalizar el SDK durante la compilación basta pasar la versión/target como argumentos:
	```sh
	./1_compile.sh 23.05.5 ath79/generic
	```
- Este repositorio incluye `tox.ini` con el entorno `py313`; los intérpretes que falten se omiten automáticamente (`skip_missing_interpreters=true`), de modo que se puede ejecutar en laptops con un solo Python instalado y en CI.
- Cuando se ejecute una rama candidata, usa el siguiente comando para asegurar que los tests pasan:
	```sh
	tox -e py313 -- --maxfail=1 --durations=10
	```

### Automatización operativa

- **Rotación de secretos:** Ejecuta la pestaña *Credentials & TLS* en LuCI para invocar `/usr/bin/mcubridge-rotate-credentials`. Esto regenera `mcubridge.general.serial_shared_secret`, refresca la contraseña del cloud, reinicia el daemon y expone el snippet `#define BRIDGE_SERIAL_SHARED_SECRET "..."`.
- **Smoke test de hardware:** Ejecuta `/usr/bin/mcubridge-hw-smoke` para validar el enlace local, credenciales y una ida y vuelta real de gRPC/IPC.
- **Harness multi-dispositivo:** Ejecuta `../tools/hardware_harness.py` en paralelo para verificar toda la flota de MCUs de forma centralizada.
- **Frame debug en Linux:** Para inspeccionar tráfico binario del enlace serie, detén `mcubridge` y ejecuta `python3 -m tools.frame_debug --port /dev/ttyATH0 --command CMD_LINK_RESET --read-response`.

## Despliegue seguro

### 0. Credenciales compartidas (daemon, CGI y scripts)

> **Nota:** El sistema no debe desplegarse con secretos placeholder. En primer boot, el instalador provisiona un `serial_shared_secret` único si falta. Rota el material desde LuCI y pega el snippet `#define BRIDGE_SERIAL_SHARED_SECRET "..."` en tu sketch.

```sh
SECRET=$(openssl rand -hex 32)
PASS=$(openssl rand -base64 24)
uci batch <<EOF
set mcubridge.general.serial_shared_secret='$SECRET'
set mcubridge.general.cloud_user='mcubridge-daemon'
set mcubridge.general.cloud_pass='$PASS'
commit mcubridge
EOF
/etc/init.d/mcubridge restart
```

### 1. Autenticación del enlace serie MCU ↔ Linux

- El handshake usa un tag HMAC-SHA256 (16 bytes) derivado de `serial_shared_secret`; si el secreto no existe o es débil, el daemon se niega a arrancar.
- Genera un secreto único por dispositivo (mínimo 8 bytes, idealmente 32) y aplícalo antes de iniciar el servicio.

### 2. Políticas de comando y acceso sensible

- `allowed_commands` controla los binarios que el daemon puede lanzar. Un valor vacío significa *ningún comando permitido*.
- Cada acción sensible se puede permitir/denegar de forma granular con:
	- `cloud_allow_file_read`, `cloud_allow_file_write`, `cloud_allow_file_remove`
	- `cloud_allow_datastore_get`, `cloud_allow_datastore_put`
	- `cloud_allow_mailbox_read`, `cloud_allow_mailbox_write`
	- `cloud_allow_shell_run`, `cloud_allow_shell_run_async`, `cloud_allow_shell_poll`, `cloud_allow_shell_kill`
	- `cloud_allow_console_input`
	- `cloud_allow_digital_read`, `cloud_allow_digital_write`, `cloud_allow_digital_mode`
	- `cloud_allow_analog_read`, `cloud_allow_analog_write`
- Configúralos en LuCI (sección **Services → McuBridge → Security**) o vía CLI:
	```sh
	uci set mcubridge.general.cloud_allow_file_write='0'
	uci set mcubridge.general.cloud_allow_mailbox_write='0'
	uci commit mcubridge && /etc/init.d/mcubridge reload
	```
- TopicAuthorization ahora opera en modo "deny-by-default": cualquier combinación no autorizada explícitamente se rechaza.

## Verificación y control de calidad

- **Tipado estático:** Ejecuta `pyright` en la raíz del repositorio.
- **Guardia del protocolo:** Corre `tox -e protocol` para asegurar consistencia del contrato binario.
- **Cobertura Python & C++:** Lanza `tox -e coverage` para obtener reportes de cobertura consolidados.
- **Métricas:** Comprueba el endpoint `/metrics` en el puerto 9130 para telemetría Prometheus.
