# Arduino MCU Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**MCU Bridge 2 es un reemplazo moderno, robusto y agnóstico de hardware para el sistema Bridge original.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en dispositivos OpenWrt, reemplazando la antigua solución basada en `python-bridge` por un daemon asíncrono y un protocolo RPC binario eficiente.

## Características Principales

- **Límites configurables:** Los buffers internos de consola y mailbox se pueden ajustar vía UCI (`console_queue_limit_bytes`, `mailbox_queue_limit`, `mailbox_queue_bytes_limit`) para prevenir desbordes. Se incluyen límites estrictos como `pending_pin_request_limit`, `file_write_max_bytes` y `file_storage_quota_bytes`.
- **Backpressure en MQTT con MQTT v5:** Control de flujo mediante `mqtt_queue_limit` y uso de propiedades MQTT v5 para negociar flujos de respuesta.
- **Respuestas correladas en MQTT:** Propagación de `correlation_data` y `response_topic` para asociaciones inequívocas entre peticiones y respuestas.
- **Seguridad Funcional (SIL-2):** Librería MCU escrita en C++11 sin STL y sin alocación dinámica, garantizando determinismo y estabilidad.
- **Protección de Flash:** Bloqueo de inicio si las rutas de escritura intensa (`file_system_root`, `mqtt_spool_dir`) no están en `/tmp` (RAM).

### Novedades (OpenWrt 25.12)

- **Detección de Hardware (Capabilities Discovery):** El protocolo incluye introspección (`CMD_GET_CAPABILITIES`) para conocer las características físicas del MCU (pines, arquitectura, features como EEPROM, DAC, FPU, I2C).
- **Integración con APK:** Paquetización moderna utilizando el sistema de paquetes **APK** de OpenWrt 25.12.
- **Transporte de Alto Rendimiento:** Uso de `python3-pyserial-asyncio-fast` para un manejo asíncrono del puerto serie con latencia mínima.
- **Logging Hexadecimal:** Todo el tráfico binario se registra mediante volcados hexadecimales en syslog.
- **Manejo de Excepciones SIL-2:** Refactorización profunda del manejo de errores para eliminar capturas genéricas.

### Novedades (noviembre 2025)

- **Compresión RLE opcional:** Payloads con datos repetitivos (buffers de LEDs, streams de sensores uniformes) pueden comprimirse con Run-Length Encoding antes de enviarlos. Implementación disponible en C++ (`rle.h`) y Python (`rle.py`), con heurísticas para decidir cuándo conviene comprimir. Ver [PROTOCOL.md §7](PROTOCOL.md#7-compresión-rle-opcional) para detalles del formato.
- Especificación única del protocolo en `../tools/protocol/spec.toml` con generador (`../tools/protocol/generate.py`) que emite `../openwrt-mcu-bridge/mcubridge/rpc/protocol.py` y `../openwrt-library-arduino/src/protocol/rpc_protocol.h`, garantizando consistencia MCU↔MPU.
- Migración del stack MQTT a **aiomqtt 2.5** + `paho-mqtt` 2.1: el daemon y los ejemplos usan un shim asíncrono compatible con la API previa de `asyncio-mqtt`, con soporte completo de MQTT v5 (propiedades de respuesta, clean start first-only, códigos de motivo enriquecidos) y reconexiones más predecibles en brokers modernos.
- **Correlación automática de peticiones MQTT v5:** todas las respuestas generadas por el daemon reutilizan `response_topic` y `correlation_data` cuando el cliente lo solicita. Además, se adjuntan propiedades de usuario (`bridge-request-topic`, `bridge-pin`, `bridge-datastore-key`, `bridge-file-path`, `bridge-process-pid`, etc.) y `message_expiry_interval` específicos por servicio para que los consumidores puedan validar el contexto original incluso cuando los mensajes pasan por brokers compartidos.
- Revisión manual de los bindings regenerados ejecutando `console_test.py`, `led13_test.py` y `datastore_test.py` del paquete `openwrt-mcu-examples-python`, confirmando compatibilidad funcional.
- Instrumentación de logging en `../openwrt-mcu-bridge/mcubridge/daemon.py` para diferenciar errores de COBS decode de fallos al parsear frames, facilitando el diagnóstico de problemas en serie.
- **Datastore MQTT sin ida y vuelta al MCU:** Las lecturas `br/datastore/get/#` ahora se resuelven íntegramente en Linux usando la caché actualizada por `CMD_DATASTORE_PUT`. Las solicitudes que terminan en `/request` reciben inmediatamente el último valor disponible o un `bridge-error=datastore-miss` (payload vacío) sin congestionar el bus serial.
- **Telemetría de colas consolidada:** `RuntimeState` ahora registra métricas de drop/truncamiento por servicio (`mqtt_dropped_messages`, `console_dropped_chunks`, `mailbox_truncated_bytes`, etc.) y el writer periódico (`status_writer`) las expone tanto en `/tmp/mcubridge_status.json` (snapshot en tmpfs; se pierde al reboot) como en los tópicos `br/system/status`, permitiendo integrar alertas en grafana/Prometheus sin parsers adicionales.
- **Persistencia vs Flash-wear:** por defecto los paths intensivos en escritura apuntan a tmpfs (`mqtt_spool_dir=/tmp/mcubridge/spool`, `file_system_root=/tmp/mcu_files`). Para persistencia, configura **solo** `file_system_root` a un mount externo (por ejemplo `/mnt/sda1/mcu_files`) vía UCI/LuCI y habilita `allow_non_tmp_paths=1`. El `mqtt_spool_dir` se mantiene siempre bajo `/tmp` para evitar desgaste de flash.
- **Spool MQTT autodiagnosticado:** Si el spool en disco detecta errores del filesystem (corrupción, disco lleno, permisos) el daemon deshabilita automáticamente la persistencia, publica `mqtt_spool_degraded`/`mqtt_spool_failure_reason` en el status JSON y continúa en modo *best effort* sin bloquear la cola de publicación en memoria.
- **Cuotas de escritura de archivos:** El sandbox ahora aplica `file_write_max_bytes` (límite por frame) y `file_storage_quota_bytes` (cuota total bajo `file_system_root`). Los rechazos devuelven `STATUS_ERROR` al MCU o `bridge-error` en MQTT con motivos `write_limit_exceeded` o `storage_quota_exceeded`, y `RuntimeState` mantiene contadores (`file_write_limit_rejections`, `file_storage_limit_rejections`, `file_storage_bytes_used`) que se exportan en los snapshots y métricas para observabilidad.
- El daemon ahora **falla en seguro** cuando `mqtt_tls=1`: si configuras un `mqtt_cafile` explícito y el archivo falta, el arranque se aborta con un error explícito. Los certificados de cliente (`mqtt_certfile`/`mqtt_keyfile`) son opcionales y solo aplican cuando el broker exige mTLS.
- La ejecución remota de comandos MQTT requiere una lista blanca explícita (`mcubridge.general.allowed_commands`). Un valor vacío significa *ningún comando permitido*; use `*` para habilitar todos de forma consciente.
- **Watchdog procd (UCI):** el init script lee `watchdog_enabled` y `watchdog_interval` desde UCI y configura `procd_set_param watchdog` en consecuencia. El daemon emite `WATCHDOG=trigger` en `stdout` con la cadencia requerida y reporta latidos en `RuntimeState`.
- **Respawn (UCI):** ajusta `respawn_threshold`, `respawn_timeout` y `respawn_retry` en UCI para endurecer o relajar la ventana de reintentos sin editar `/etc/init.d/mcubridge`.
- **Logging estructurado + Prometheus:** todos los módulos `mcubridge.*` ahora emiten líneas JSON con `ts`, `level`, `logger`, `message` y `extra`, facilitando la ingesta directa en syslog, Loki o Elastic. Además, se añadió un exportador HTTP opcional (`metrics_enabled`, `metrics_host`, `metrics_port`) que expone el mismo snapshot de `RuntimeState` en formato Prometheus sin dejar de publicar `br/system/metrics` vía MQTT.
- **Enlace serie con autenticación estricta:** el handshake `CMD_LINK_RESET`/`CMD_LINK_SYNC` ahora se valida en ambos extremos: la librería Arduino exige definir `BRIDGE_SERIAL_SHARED_SECRET` (o compilar con `BRIDGE_ALLOW_INSECURE_SERIAL_SECRET` en entornos de laboratorio) y el daemon rechaza cualquier frame que no sea de handshake o estado hasta que la sincronización se complete exitosamente. Todos los parámetros derivan de `../tools/protocol/spec.toml`, por lo que debes regenerar el protocolo tras modificar el spec.
- **Cuotas de peticiones pendientes:** `RuntimeState` expone `pending_pin_request_limit` (configurable vía UCI) para evitar que MQTT u otros productores saturen las colas de lecturas GPIO; si se supera el límite, el daemon responde con `bridge-error=pending-pin-overflow` y no emite el comando al MCU, manteniendo el enlace libre de ataques por agotamiento.
- **Watchdog de firmware opcional:** la librería Arduino habilita el WDT hardware (2 s por defecto) al inicializarse en AVR, con `wdt_reset()` aplicado en cada ciclo de `Bridge.process()`. Define `BRIDGE_ENABLE_WATCHDOG 0` o personaliza `BRIDGE_WATCHDOG_TIMEOUT` antes de incluir `Bridge.h` si necesitas desactivarlo o ajustar el intervalo.
- **Guía rápida de UCI**:
	```sh
	uci set mcubridge.general.mqtt_tls='1'
	# Opcional: cafile (si está vacío se usa el trust store del sistema)
	uci set mcubridge.general.mqtt_cafile='/etc/ssl/certs/bridge-ca.pem'
	# Opcional (mTLS): solo si tu broker exige certificados de cliente
	uci set mcubridge.general.mqtt_certfile='/etc/mcubridge/client.crt'
	uci set mcubridge.general.mqtt_keyfile='/etc/mcubridge/client.key'
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
	- El instalador (`3_install.sh`) inicializa los defaults de runtime y secretos, pero ya no genera certificados de cliente; configura TLS/mTLS vía UCI/LuCI según tu broker.

## Plan de compatibilidad y toolchain

| Capa | Estado actual | Próximo paso controlado | Cómo se valida |
| --- | --- | --- | --- |
| Python (daemon en el MPU) | Base en Python 3.13.9-r2. | Mantener compatibilidad con futuras versiones. | `tox -e py313` |
| Toolchain OpenWrt | SDK 25.12.0 final. | Compilación de paquetes APK. | `./1_compile.sh` |
| MCU Firmware | C++11 / ETL (SIL-2). | Cobertura extrema sin STL. | `./tools/coverage_arduino.sh` |

- Para personalizar el SDK durante la compilación basta pasar la versión/target como argumentos:
	```sh
	./1_compile.sh 23.05.5 ath79/generic
	```
	Esto reutiliza el pipeline de descarga y sincronización pero apunta al `gcc` publicado junto con OpenWrt 23.05 (genera IPKs), lo que permite medir divergencias respecto al build predeterminado (25.12.0-rc2 que genera APKs).
- Este repositorio incluye `tox.ini` con el entorno `py313`; los intérpretes que falten se omiten automáticamente (`skip_missing_interpreters=true`), de modo que se puede ejecutar en laptops con un solo Python instalado y en CI.
- Cuando se ejecute una rama candidata, usa el siguiente comando para asegurar que los tests pasan:
	```sh
	tox -e py313 -- --maxfail=1 --durations=10
	```
- Los reportes de cobertura para el firmware siguen saliendo de `tools/coverage_arduino.sh`, que deja registrado qué versión exacta de `avr-g++` ejecutó.

### Automatización operativa

- **Rotación de secretos:** Ejecuta `../tools/rotate_credentials.sh --host <mcu>` o usa la pestaña *Credentials & TLS* en LuCI para invocar `/usr/bin/mcubridge-rotate-credentials`. Ambas rutas regeneran `mcubridge.general.serial_shared_secret`, refrescan la contraseña MQTT, reinician el daemon y terminan imprimiendo el snippet `#define BRIDGE_SERIAL_SHARED_SECRET "..."` para que lo pegues al inicio de tu sketch antes de incluir `Bridge.h`.
- **Smoke test de hardware:** Lanza `../tools/hardware_smoke_test.sh --host <mcu>` (o el botón *Run smoke test* en LuCI) para ejecutar `/usr/bin/mcubridge-hw-smoke`, que valida servicio, credenciales y una ida y vuelta real a `br/system/status`.
- **Harness multi-dispositivo:** Copia `../hardware/targets.example.toml` a `../hardware/targets.toml`, ajusta tus hosts y luego ejecuta `../tools/hardware_harness.py --list` para verlos. El mismo script corre las pruebas en paralelo (`--max-parallel 4`), filtra por `--tag staging` o `--target lab-mcu-01` y expone reportes JSON (`--json results/hw-smoke.json`) ideales para CI.
- **TLS guiado:** La pestaña *Credentials & TLS* documenta cómo subir bundles `tar.gz` con CA/cert/key a `/etc/mcubridge/tls/` usando `scp` antes de apuntar el daemon al nuevo material.
- **Frame debug en Linux:** Cuando necesites replicar el ejemplo `FrameDebug` del sketch pero del lado del daemon, detén `mcubridge` y ejecuta `python3 -m tools.frame_debug --port /dev/ttyATH0 --command CMD_LINK_RESET --read-response`. El utilitario construye el frame con las mismas rutinas (`rpc.Frame`, `cobs.encode`), imprime CRC/tamaños/hex y, si `--read-response` está activo, decodifica el primer frame que responda el MCU. También puedes omitir `--port` para inspeccionar la estructura del frame sin tocar el hardware o cambiar `--payload`/`--count` para generar ráfagas a intervalos fijos.

## Despliegue seguro

### 0. Credenciales compartidas (daemon, CGI y scripts)

> **Nota:** El sistema no debe desplegarse con secretos placeholder. En primer boot, `uci-defaults`/el instalador provisionan un `serial_shared_secret` único si falta; además `RuntimeConfig.__post_init__` rechaza explícitamente el placeholder histórico `changeme123`. Rota el material con `../tools/rotate_credentials.sh` o desde LuCI y pega el snippet `#define BRIDGE_SERIAL_SHARED_SECRET "..."` en tu sketch antes de exponer el equipo. Consulta la guía completa en [`CREDENTIALS.md`](CREDENTIALS.md).

	```sh
	SECRET=$(openssl rand -hex 32)
	PASS=$(openssl rand -base64 24)
	uci batch <<EOF
	set mcubridge.general.serial_shared_secret='$SECRET'
	set mcubridge.general.mqtt_user='mcubridge-daemon'
	set mcubridge.general.mqtt_pass='$PASS'
	commit mcubridge
	EOF
	/etc/init.d/mcubridge restart
	```
- También puedes usar `../tools/rotate_credentials.sh --host <mcu>` o la pestaña *Credentials & TLS* (que imprime el snippet `#define BRIDGE_SERIAL_SHARED_SECRET`) para ejecutar ese procedimiento remotamente mediante `/usr/bin/mcubridge-rotate-credentials`, que ahora actualiza UCI directamente.
- `3_install.sh` ya no genera material TLS (CA/cert/key) para el bridge. Para TLS usa un certificado del broker confiable por el sistema (p.ej. Let's Encrypt) o configura `mqtt_cafile` apuntando a tu CA. Para mTLS, provee explícitamente `mqtt_certfile`/`mqtt_keyfile` y configura el broker para requerirlos.
### 1. Autenticación del enlace serie MCU ↔ Linux

- El handshake usa un tag HMAC-SHA256 (16 bytes) derivado de `serial_shared_secret`; si el secreto no existe o es débil, el daemon se niega a arrancar.
- Genera un secreto único por dispositivo (mínimo 8 bytes, idealmente 32) y aplícalo antes de iniciar el servicio:
	```sh
	openssl rand -hex 32 | awk '{print tolower($0)}' \
	  | uci set mcubridge.general.serial_shared_secret="$(cat)"
	uci commit mcubridge
	/etc/init.d/mcubridge restart
	```
- Configura el secreto únicamente en UCI (`mcubridge.general.serial_shared_secret`) y mantenlo sincronizado con el `#define BRIDGE_SERIAL_SHARED_SECRET "..."` del sketch.
- En el sketch define `#define BRIDGE_SERIAL_SHARED_SECRET "..."` (o usa el snippet que muestra LuCI) y vuelve a cargar el firmware; sin esto, el MCU rechazará el handshake. Genera/rota un secreto por dispositivo antes de producción (por ejemplo con `../tools/rotate_credentials.sh`).

### 2. Políticas de comando y topics sensibles

- `allowed_commands` controla los binarios que el daemon puede lanzar vía MQTT o el MCU. Un valor vacío significa *ningún comando permitido*; evita usar `*` salvo en laboratorios.
- Cada acción MQTT sensible ahora se puede permitir/denegar de forma granular con:
	- `mqtt_allow_file_read`, `mqtt_allow_file_write`, `mqtt_allow_file_remove`
	- `mqtt_allow_datastore_get`, `mqtt_allow_datastore_put`
	- `mqtt_allow_mailbox_read`, `mqtt_allow_mailbox_write`
	- `mqtt_allow_shell_run`, `mqtt_allow_shell_run_async`, `mqtt_allow_shell_poll`, `mqtt_allow_shell_kill`
	- `mqtt_allow_console_input`
	- `mqtt_allow_digital_read`, `mqtt_allow_digital_write`, `mqtt_allow_digital_mode`
	- `mqtt_allow_analog_read`, `mqtt_allow_analog_write`
- Configúralos en LuCI (sección **Services → McuBridge → Security**) o vía CLI:
	```sh
	uci set mcubridge.general.mqtt_allow_file_write='0'
	uci set mcubridge.general.mqtt_allow_mailbox_write='0'
	uci commit mcubridge && /etc/init.d/mcubridge reload
	```
- TopicAuthorization ahora opera en modo "deny-by-default": cualquier combinación topic/acción no listada arriba se rechaza y se publica `topic-action-forbidden`.
- Cuando una acción está bloqueada, el daemon publica `bridge-error=topic-action-forbidden` en `br/system/status`, de modo que los consumidores reciben un fallo explícito.

### 3. Recomendaciones para ACLs MQTT

- Crea credenciales dedicadas para el daemon con permiso de publicar en `br/#` y subscribirse únicamente a los prefijos configurados.
- Limita a los clientes externos para que solo puedan `PUBLISH` en los topics necesarios (`br/d/+/mode`, `br/datastore/put/...`, etc.) y nunca en `system/status` o `sh/run_async`.
- En brokers tipo Mosquitto:
	```
	# /etc/mosquitto/acl
	user mcubridge-daemon
	topic readwrite br/#

	user sensors-ro
	topic write br/d/+/read
	topic read br/system/#
	```
- Usa TLS mutuo siempre que sea posible y valida que el CA configurado en `mqtt_cafile` coincida con el que utiliza el broker.

### 4. Procedimiento sugerido de rollout

1. Compila e instala los nuevos paquetes (`./1_compile.sh`, `./3_install.sh`) en un nodo de staging.
2. Actualiza el secreto serie y las políticas (`allowed_commands`, `mqtt_allow_*`) con UCI o LuCI.
3. Re-flashea el sketch Arduino con la librería regenerada para que comparta el mismo secreto y protocolo.
4. Reinicia el daemon y valida el handshake (`logread -f | grep handshake`).
5. Verifica ACLs MQTT ejecutando un cliente no autorizado y confirmando que recibe `topic-action-forbidden` o un rechazo del broker.
6. Repite en producción siguiendo un orden controlado (primero brokers/ACLs, luego MCU, finalmente daemon) para minimizar downtime.

> **Consejo:** Automatiza los pasos 2–5 con Ansible o un script de LuCI RPC para asegurar consistencia entre flotas.
- Nuevo sistema de buffering persistente para `CMD_PROCESS_POLL_RESP`, evitando pérdidas cuando el proceso supera `MAX_PAYLOAD_SIZE` en una sola lectura.
- Se añadieron colas de estado en `RuntimeState` para reportar con precisión la finalización de procesos y los flags de truncamiento vía MQTT.
- Los endpoints REST (`pin_rest_cgi.py`) y la API de LuCI vuelven a publicar comandos MQTT con reintentos exponenciales y límites de tiempo configurables, entregando mejor UX ante brokers lentos.

## Arquitectura

- **Callbacks de estado:** Registra `Bridge.onStatus(...)` en tus sketches para recibir `STATUS_*` desde Linux, incluyendo mensajes de error descriptivos cuando una operación (p.ej. I/O de archivos) falla.
1.  **`openwrt-mcu-bridge`**: El daemon principal de Python que se ejecuta en el MPU.
2.  **`openwrt-library-arduino`**: La librería C++ para el sketch que se ejecuta en el MCU.
3.  **`luci-app-mcubridge`**: La interfaz de configuración web.
4.  **`openwrt-mcu-examples-python`**: Paquete cliente con ejemplos de uso.
5.  **`openwrt-mcu-core`**: Ficheros de configuración base del sistema.

> ¿Buscas detalles adicionales sobre flujos internos, controles de seguridad, observabilidad y el contrato del protocolo? Revisa [`PROTOCOL.md`](PROTOCOL.md) para obtener el documento actualizado.

> **Nota:** Todas las dependencias del daemon se compilan localmente como APKs en `feeds/` y se instalan desde `bin/` durante `3_install.sh`. Las librerías Python (`aiomqtt`, `paho-mqtt`, `cobs`, `prometheus-client`, `psutil`) tienen sus propios Makefiles en el feed `mcubridge`. El inventario completo vive en `../requirements/runtime.toml`; ejecuta `./tools/sync_runtime_deps.py` tras modificarlo para regenerar `requirements/runtime.txt` y refrescar los Makefiles.
>
> **✅ Nota sobre pyserial:** La dependencia `pyserial` ha sido reemplazada por una implementación pura basada en `termios` (módulo built-in de Python). Esto elimina la dependencia externa y evita posibles incompatibilidades con futuras versiones de Python. Ver `mcubridge/transport/termios_serial.py`.

## Primeros Pasos

### Opción A: Imagen completa (recomendado para instalaciones nuevas)

La forma más sencilla es compilar una imagen OpenWrt completa que ya incluye todo el ecosistema McuBridge:

1.  **Compilar imagen:** Ejecuta `./0_image.sh` para generar una imagen OpenWrt con:
	- UART a 115200 baud (corrige el baudrate legacy de 250000)
	- Todos los paquetes McuBridge preinstalados
	- Configuración automática de extroot/swap en primer boot
	- Generación automática de secretos de seguridad
2.  **Flashear:** Usa la imagen `sysupgrade` o `factory` generada en `openwrt-build/bin/targets/ath79/generic/`.
3.  **Primer boot:** Inserta una tarjeta SD y el sistema la configurará automáticamente como extroot. Después de un reinicio automático, el sistema estará listo.
4.  **Obtener secreto:** Ejecuta `uci get mcubridge.general.serial_shared_secret` y usa el valor en tu sketch Arduino.

### Opción B: Instalación sobre OpenWrt existente

Si ya tienes OpenWrt 25.12 instalado:

1.  **Compilar paquetes:** Ejecuta `./1_compile.sh` para preparar el SDK y compilar los paquetes **APK**.
2.  **Preparar almacenamiento:** Ejecuta `./2_expand.sh` para configurar extroot.
3.  **Instalar:** Transfiere los APKs y ejecuta `./3_install.sh`.

### Configuración post-instalación

1.  **Configurar:** Accede a la interfaz web de LuCI en tu MCU, navega a `Services > McuBridge` y configura el daemon. Antes de ponerlo en producción usa la pestaña *Credentials & TLS* (o `../tools/rotate_credentials.sh --host <mcu>`) para rotar el secreto serie y las credenciales MQTT directamente en UCI.
2.  **Explorar:** Revisa los ejemplos en `openwrt-mcu-examples-python/` para aprender a interactuar con el puente a través de MQTT.

### Verificación y control de calidad

- **Tipado estático:** Ejecuta `pyright` en la raíz del repositorio antes de enviar parches; la configuración (`pyrightconfig.json`) está preparada para ignorar los ejemplos legacy y validar el daemon y sus utilidades.
- **Guardia del protocolo:** Corre `tox -e protocol` (o `python tools/protocol/generate.py --check`) para asegurarte de que `../tools/protocol/spec.toml` coincide con los bindings generados en Python y C++. Este objetivo falla si olvidaste ejecutar el generador después de editar la especificación o el script.
- **Cobertura Python:** Lanza `../tools/coverage_python.sh` (o simplemente `tox -e coverage`, que encadena ambos scripts) para generar `coverage/python/` con reportes `term-missing`, `coverage.xml` y HTML. Puedes pasar argumentos extra (por ejemplo un subconjunto de tests) y usar `--output-root` si necesitas otro directorio.
- **Cobertura C++:** Ejecuta `./tools/coverage_arduino.sh` o reutiliza el `tox -e coverage` anterior para compilar un harness host que prueba el protocolo binario con `g++ -fprofile-arcs -ftest-coverage`, ejecuta los tests y genera reportes en `coverage/arduino/`. Si ya tienes un build instrumentado diferente, usa `--build-dir`/`--output-root` o `--force-rebuild` para reutilizarlo.
- **Resumen automático:** Después de correr ambos scripts, ejecuta `../tools/coverage_report.py` para generar una tabla consolidada (`coverage/coverage-summary.md` + `.json`). El workflow de CI hace esto automáticamente y publica la tabla en el *GitHub Step Summary*, además de adjuntar el markdown como artefacto.
- **Artefactos de cobertura en CI:** El workflow `coverage` de GitHub Actions corre `tox -e coverage` en Python 3.13 y publica los directorios `coverage/python` y `coverage/arduino` como artefactos para cada PR/commit, facilitando su inspección sin levantar un entorno local.
- **Matriz Python 3.13:** Usa `tox` para ejecutar la suite completa y detectar regresiones antes de desplegar:
	```sh
	tox -e py313
	```
- **Smoke test remoto:** `./tools/hardware_smoke_test.sh --host <mcu>` invoca `/usr/bin/mcubridge-hw-smoke` vía SSH y falla si el daemon no responde a `br/system/status` en menos de 7 segundos.
- **Harness multi-dispositivo:** `./tools/hardware_harness.py --manifest hardware/targets.toml --max-parallel 3 --tag regression` recorre todos los dispositivos definidos en el manifiesto, ejecuta el script anterior mediante SSH y al final resume qué nodos pasaron/ fallaron (además de producir un reporte JSON opcional).
- **Pruebas manuales:** Tras instalar los paquetes en tu MCU, verifica el flujo end-to-end ejecutando uno de los scripts de `openwrt-mcu-examples-python` y revisa los logs del daemon con `logread | grep mcubridge`.
- **Diagnóstico en el MCU:** Carga el sketch `openwrt-library-arduino/examples/FrameDebug/FrameDebug.ino` para imprimir cada 5 s el snapshot de transmisión y confirmar que `expected_serial_bytes` coincide con `last_write_return`.
- **Monitoreo:** El daemon expone estados y errores del MCU en `br/system/status` (JSON) y publica el tamaño actual de la cola MQTT en `/tmp/mcubridge_status.json` junto al límite configurado. Ese snapshot ahora incluye `mqtt_spool_*`, `watchdog_*` (latido, intervalo, habilitado) y los nuevos contadores de almacenamiento (`file_storage_bytes_used`, `file_write_limit_rejections`, `file_storage_limit_rejections`) para que LuCI los muestre sin parsers adicionales. Además, `br/system/metrics` adjunta las mismas claves y propiedades MQTT (`bridge-spool`, `bridge-watchdog-enabled`, `bridge-watchdog-interval`) para que los consumidores puedan alertar cuando el spool persistente se degrada, el watchdog deja de latir o el sandbox de archivos se acerca al límite.
- **Telemetría reforzada:** `RuntimeState` cuenta los eventos de drop y truncamiento en todas las colas (MQTT, consola, mailbox y mailbox_incoming) y el writer periódico exporta los acumuladores en `/tmp/mcubridge_status.json` (`*_dropped_*`, `*_truncated_*`) junto con los tamaños actuales. Estos mismos contadores se publican en `br/system/status` para integrarse con dashboards MQTT.
- **Snapshots `br/system/bridge/*`:** ahora puedes consultar el estado del enlace serie sin inspeccionar archivos locales. Publica un mensaje vacío en `br/system/bridge/handshake/get` o `br/system/bridge/summary/get` (MQTT v5 opcionalmente con `response_topic`) y el daemon responderá con `.../handshake/value` o `.../summary/value` en JSON (`content-type: application/json`). Incluye sincronización actual, contadores de handshake, versión del MCU, pipeline serial en curso y el último comando completado, además de adjuntar la propiedad de usuario `bridge-snapshot` para que los clientes puedan enrutar la respuesta.
- **Snapshots `br/system/bridge/*`:** ahora puedes consultar el estado del enlace serie sin inspeccionar archivos locales. Publica un mensaje vacío en `br/system/bridge/handshake/get` o `br/system/bridge/summary/get` (MQTT v5 opcionalmente con `response_topic`) y el daemon responderá con `.../handshake/value` o `.../summary/value` en JSON (`content-type: application/json`). Incluye sincronización actual, contadores de handshake, versión del MCU, pipeline serial en curso y el último comando completado, además de adjuntar la propiedad de usuario `bridge-snapshot` para que los clientes puedan enrutar la respuesta. Además, el daemon republica estos snapshots de forma automática usando `aiocron`: `bridge_summary_interval` (60 s por defecto) controla la cadencia del resumen y `bridge_handshake_interval` (300 s por defecto) decide cada cuánto se emite el estado detallado del handshake. Ajusta ambos valores con `uci set mcubridge.general.bridge_summary_interval='<segundos>'`/`bridge_handshake_interval` y recarga el servicio.
- **Exportador Prometheus:** Habilita `uci set mcubridge.general.metrics_enabled='1'` para exponer `http://<host>:<metrics_port>/metrics` con `Content-Type: text/plain; version=0.0.4`. Ajusta `metrics_host`/`metrics_port` si necesitas escuchar en otra interfaz. Verifica con `curl http://127.0.0.1:9130/metrics` y busca gauges como `mcubridge_mqtt_queue_size` o `mcubridge_serial_decode_errors`. Los campos de texto se representan como `mcubridge_info{key="...",value="..."} 1`.