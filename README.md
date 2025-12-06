# Arduino Yún Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**Yún Bridge 2 es un reemplazo moderno, robusto y de alto rendimiento para el sistema Bridge original de Arduino Yún.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en el Yún, reemplazando la antigua solución basada en `python-bridge` por un daemon asíncrono y un protocolo RPC binario eficiente.

## Características Principales

- **Límites configurables:** Los buffers interno de consola y mailbox se pueden ajustar vía UCI (`console_queue_limit_bytes`, `mailbox_queue_limit`, `mailbox_queue_bytes_limit`) para prevenir desbordes en escenarios con alto tráfico. A partir de noviembre 2025 también puedes fijar `pending_pin_request_limit` para controlar cuántas lecturas de GPIO quedan en vuelo entre MQTT y el MCU sin bloquear el bus serial.
	- Todos los valores por defecto consumidos por el daemon viven ahora en `openwrt-yun-bridge/yunbridge/const.py` (`DEFAULT_MQTT_PORT`, `DEFAULT_SERIAL_RETRY_TIMEOUT`, etc.), lo que evita duplicar literales entre módulos y pruebas.
- **Backpressure en MQTT con MQTT v5:** El tamaño de la cola de publicación hacia el broker se controla con `mqtt_queue_limit`, mientras que las conexiones salientes usan propiedades MQTT v5 (session expiry = 0, request/response info) para que los clientes sepan cuándo reiniciar suscripciones y puedan negociar flujos de respuesta.
- **Respuestas correladas en MQTT:** Cada publicación originada por el daemon puede reutilizar el `response_topic` proporcionado por el cliente y propaga un `correlation_data` binario, de modo que las respuestas (GPIO, datastore, mailbox, sistema, procesos, etc.) se pueden asociar de manera inequívoca incluso si varios consumidores comparten el mismo prefijo. Las propiedades de usuario (`bridge-request-topic`, `bridge-pin`, `bridge-datastore-key`, `bridge-file-path`, `bridge-process-pid`, `bridge-status`, …) transportan metadatos humanamente legibles sin romper la compatibilidad con clientes MQTT v3.
- **Handshake automático MCU ↔ Linux:** Tras cada reconexión, el daemon solicita `CMD_GET_VERSION` y publica la versión del firmware del sketch en `br/system/version/value`, de modo que los clientes pueden validar compatibilidad antes de ejecutar comandos.
- **Protección ante frames serie malformados:** El lector COBS aplica un límite duro al tamaño de cada paquete y envía `STATUS_MALFORMED` al MCU cuando se detecta un frame que supera la especificación, evitando que un sketch ruidoso deje bloqueado el bucle asíncrono en Linux.
- **Procesos asíncronos robustos:** Los polls sucesivos ahora entregan todo el `stdout`/`stderr` generado, incluso cuando los procesos producen más datos que un frame. El daemon mantiene buffers circulares por PID, conserva el `exit_code` hasta que el MCU confirma la lectura completa y vigila cada proceso en segundo plano para liberar el slot concurrente incluso si el cliente MQTT nunca vuelve a emitir `PROCESS_POLL`.
- **Estado inmediato de buzón:** Los sketches pueden invocar `Mailbox.requestAvailable()` y recibir el conteo pendiente en `Bridge.onMailboxAvailableResponse`, lo que evita lecturas vacías y mantiene sincronizado al MCU con la cola de Linux.
- **Lecturas de pin dirigidas desde Linux:** `CMD_DIGITAL_READ`/`CMD_ANALOG_READ` solo se originan desde el daemon; si un sketch invoca `Bridge.requestDigitalRead()`/`Bridge.requestAnalogRead()` ahora recibirá `STATUS_NOT_IMPLEMENTED` con `bridge-error=pin_read_initiate_from_linux`, evitando que el MCU monopolice el enlace serial con peticiones legacy.

### Novedades (noviembre 2025)

- Especificación única del protocolo en `tools/protocol/spec.toml` con generador (`tools/protocol/generate.py`) que emite `openwrt-yun-bridge/yunbridge/rpc/protocol.py` y `openwrt-library-arduino/src/protocol/rpc_protocol.h`, garantizando consistencia MCU↔MPU.
- Migración del stack MQTT a **aiomqtt 2.4** + `paho-mqtt` 2.1: el daemon y los ejemplos usan un shim asíncrono compatible con la API previa de `asyncio-mqtt`, con soporte completo de MQTT v5 (propiedades de respuesta, clean start first-only, códigos de motivo enriquecidos) y reconexiones más predecibles en brokers modernos.
- **Correlación automática de peticiones MQTT v5:** todas las respuestas generadas por el daemon reutilizan `response_topic` y `correlation_data` cuando el cliente lo solicita. Además, se adjuntan propiedades de usuario (`bridge-request-topic`, `bridge-pin`, `bridge-datastore-key`, `bridge-file-path`, `bridge-process-pid`, etc.) y `message_expiry_interval` específicos por servicio para que los consumidores puedan validar el contexto original incluso cuando los mensajes pasan por brokers compartidos.
- Revisión manual de los bindings regenerados ejecutando `console_test.py`, `led13_test.py` y `datastore_test.py` del paquete `openwrt-yun-examples-python`, confirmando compatibilidad funcional.
- Instrumentación de logging en `yunbridge/daemon.py` para diferenciar errores de COBS decode de fallos al parsear frames, facilitando el diagnóstico de problemas en serie.
- **Datastore MQTT sin ida y vuelta al MCU:** Las lecturas `br/datastore/get/#` ahora se resuelven íntegramente en Linux usando la caché actualizada por `CMD_DATASTORE_PUT`. Las solicitudes que terminan en `/request` reciben inmediatamente el último valor disponible o un `bridge-error=datastore-miss` (payload vacío) sin congestionar el bus serial.
- **Telemetría de colas consolidada:** `RuntimeState` ahora registra métricas de drop/truncamiento por servicio (`mqtt_dropped_messages`, `console_dropped_chunks`, `mailbox_truncated_bytes`, etc.) y el writer periódico (`status_writer`) las expone tanto en `/tmp/yunbridge_status.json` como en los tópicos `br/system/status`, permitiendo integrar alertas en grafana/Prometheus sin parsers adicionales.
- **Spool MQTT autodiagnosticado:** Si el spool en disco detecta errores del filesystem (corrupción, disco lleno, permisos) el daemon deshabilita automáticamente la persistencia, publica `mqtt_spool_degraded`/`mqtt_spool_failure_reason` en el status JSON y continúa en modo *best effort* sin bloquear la cola de publicación en memoria.
- El daemon ahora **falla en seguro** cuando `mqtt_tls=1`: si falta el CA o el certificado cliente, el arranque se aborta con error explícito.
- La ejecución remota de comandos MQTT requiere una lista blanca explícita (`yunbridge.general.allowed_commands`). Un valor vacío significa *ningún comando permitido*; use `*` para habilitar todos de forma consciente.
- **Keepalive de watchdog integrado:** cuando `procd` expone `PROCD_WATCHDOG` (configurado por defecto a `10000` ms en `yunbridge.init` para producir pulsos cada 5 s) o exportas `YUNBRIDGE_WATCHDOG_INTERVAL`, el daemon lanza un `WatchdogKeepalive` asíncrono que pulsa `WATCHDOG=trigger` en `stdout` y reporta los latidos en `RuntimeState`. Ajusta `procd_set_param watchdog` si necesitas ventanas más estrictas; el daemon emitirá pulsos cada mitad del valor negociado.
- Puedes desactivar la integración del watchdog definiendo `YUNBRIDGE_DISABLE_PROCD_WATCHDOG=1` antes de iniciar el servicio o ajustar la ventana exportando `YUNBRIDGE_PROCD_WATCHDOG_MS=<milisegundos>` (el init script propagará ese valor al daemon para que mantenga la cadencia correcta).
- **Logging estructurado + Prometheus:** todos los módulos `yunbridge.*` ahora emiten líneas JSON con `ts`, `level`, `logger`, `message` y `extra`, facilitando la ingesta directa en syslog, Loki o Elastic. Además, se añadió un exportador HTTP opcional (`metrics_enabled`, `metrics_host`, `metrics_port`) que expone el mismo snapshot de `RuntimeState` en formato Prometheus sin dejar de publicar `br/system/metrics` vía MQTT.
- **Enlace serie con autenticación estricta:** el handshake `CMD_LINK_RESET`/`CMD_LINK_SYNC` ahora se valida en ambos extremos: la librería Arduino exige definir `BRIDGE_SERIAL_SHARED_SECRET` (o compilar con `BRIDGE_ALLOW_INSECURE_SERIAL_SECRET` en entornos de laboratorio) y el daemon rechaza cualquier frame que no sea de handshake o estado hasta que la sincronización se complete exitosamente. Todos los parámetros derivan de `tools/protocol/spec.toml`, por lo que debes regenerar el protocolo tras modificar el spec.
- **Cuotas de peticiones pendientes:** `RuntimeState` expone `pending_pin_request_limit` (configurable vía UCI) para evitar que MQTT u otros productores saturen las colas de lecturas GPIO; si se supera el límite, el daemon responde con `bridge-error=pending-pin-overflow` y no emite el comando al MCU, manteniendo el enlace libre de ataques por agotamiento.
- **Watchdog de firmware opcional:** la librería Arduino habilita el WDT hardware (2 s por defecto) al inicializarse en AVR, con `wdt_reset()` aplicado en cada ciclo de `Bridge.process()`. Define `BRIDGE_ENABLE_WATCHDOG 0` o personaliza `BRIDGE_WATCHDOG_TIMEOUT` antes de incluir `Bridge.h` si necesitas desactivarlo o ajustar el intervalo.
- **Guía rápida de UCI**:
	```sh
	export YUNBRIDGE_SERIAL_RETRY_TIMEOUT='0.75'
	export YUNBRIDGE_SERIAL_RETRY_ATTEMPTS='3'
	export YUNBRIDGE_SERIAL_RESPONSE_TIMEOUT='3.0'
	uci set yunbridge.general.mqtt_tls='1'
	uci set yunbridge.general.mqtt_cafile='/etc/ssl/certs/bridge-ca.pem'
	uci set yunbridge.general.mqtt_certfile='/etc/ssl/certs/bridge.crt'
	uci set yunbridge.general.mqtt_keyfile='/etc/ssl/private/bridge.key'
	uci set yunbridge.general.allowed_commands='ls cat uptime'
	uci set yunbridge.general.serial_retry_timeout='0.75'
	uci set yunbridge.general.serial_retry_attempts='3'
	uci set yunbridge.general.serial_response_timeout='3.0'
	uci commit yunbridge
	```
	- Usa `allowed_commands='*'` solo en entornos controlados; cualquier otro valor se normaliza a minúsculas y se interpreta como lista explícita.
	- Las rutas de certificados deben existir; de lo contrario, el daemon abortará el arranque.
	- El instalador (`3_install.sh`) inicializa estos valores si aún no existen; personalízalos antes de ejecutar el daemon exportando `YUNBRIDGE_SERIAL_RETRY_TIMEOUT` o `YUNBRIDGE_SERIAL_RETRY_ATTEMPTS`.

## Plan de compatibilidad y toolchain

| Capa | Estado actual | Próximo paso controlado | Cómo se valida |
| --- | --- | --- | --- |
| Python (daemon en el MPU) | Base en Python 3.11.x (lo que entrega OpenWrt 23.05). | Mantener compatibilidad hacia adelante ejecutando la suite completa en 3.12 antes de promover cambios. | `tox -e py311,py312` (nuevo `tox.ini`) ejecuta `pytest` sobre `openwrt-yun-bridge/tests` con ambas versiones. |
| Toolchain OpenWrt/AVR | `1_compile.sh` descarga por defecto el SDK 24.10.4 (`gcc-13.3.0` para `ath79/generic`). | Validar paridad con la rama estable 23.05 (`gcc-13.x`) antes de hacer release, compilando ambos artefactos. | `./1_compile.sh 24.10.4` (por defecto) y `./1_compile.sh 23.05.5` generan IPKs equivalentes para comparar tamaño y ABI. |
| Librería Arduino/host harness | `tools/coverage_arduino.sh` construye el harness con el `g++` disponible en `PATH`. | Ensayar la misma versión de GCC usada en la Yún final ejecutando el script dentro del SDK/contendor deseado o adelantando el `PATH` al toolchain adecuado. | `PATH=/opt/openwrt-sdk/staging_dir/toolchain-*/bin:$PATH ./tools/coverage_arduino.sh` recompila y reporta diferencias de warnings o cobertura. |

- Para personalizar el SDK durante la compilación basta pasar la versión/target como argumentos:
	```sh
	./1_compile.sh 23.05.5 ath79/generic
	```
	Esto reutiliza el pipeline de descarga y sincronización pero apunta al `gcc` publicado junto con OpenWrt 23.05, lo que permite medir divergencias respecto al build predeterminado (24.10.4 `gcc-13.3`).
- Este repositorio incluye `tox.ini` con los entornos `py311` y `py312`; los intérpretes que falten se omiten automáticamente (`skip_missing_interpreters=true`), de modo que se puede ejecutar en laptops con un solo Python instalado y en CI multi-versión.
- Cuando se ejecute una rama candidata, usa el siguiente comando para asegurar que ambos intérpretes comparten resultados:
	```sh
	tox -e py311,py312 -- --maxfail=1 --durations=10
	```
- Los reportes de cobertura para el firmware siguen saliendo de `tools/coverage_arduino.sh`, que deja registrado qué versión exacta de `avr-g++` ejecutó, facilitando la correlación con los builds de OpenWrt 23.05.

### Automatización operativa

- **Rotación de secretos:** Ejecuta `tools/rotate_credentials.sh --host <yun>` o usa la pestaña *Credentials & TLS* en LuCI para invocar `/usr/bin/yunbridge-rotate-credentials`. Ambas rutas generan un `YUNBRIDGE_SERIAL_SECRET` nuevo, refrescan la contraseña MQTT, reinician el daemon y terminan imprimiendo el snippet `#define BRIDGE_SERIAL_SHARED_SECRET "..."` para que lo pegues al inicio de tu sketch antes de incluir `Bridge.h`.
- **Smoke test de hardware:** Lanza `tools/hardware_smoke_test.sh --host <yun>` (o el botón *Run smoke test* en LuCI) para ejecutar `/usr/bin/yunbridge-hw-smoke`, que valida servicio, credenciales y una ida y vuelta real a `br/system/status`.
- **Harness multi-dispositivo:** Copia `hardware/targets.example.toml` a `hardware/targets.toml`, ajusta tus hosts y luego ejecuta `tools/hardware_harness.py --list` para verlos. El mismo script corre las pruebas en paralelo (`--max-parallel 4`), filtra por `--tag staging` o `--target lab-yun-01` y expone reportes JSON (`--json results/hw-smoke.json`) ideales para CI.
- **TLS guiado:** La pestaña *Credentials & TLS* documenta cómo subir bundles `tar.gz` con CA/cert/key a `/etc/yunbridge/tls/` usando `scp` antes de apuntar el daemon al nuevo material.

### Resolver errores de `aiomqtt` / `paho-mqtt`

- Para evitar el bug del feed oficial (que arrastra `python3-hatchling` como dependencia de runtime), `3_install.sh` ahora instala `aiomqtt>=2.4,<3.0` y `paho-mqtt>=2.1,<3.0` directamente desde PyPI usando `pip3`.
- Si el dispositivo no tenía `pip3`, el instalador añade automáticamente `python3-pip` antes de continuar.
- Si la instalación falla por certificados o espacio en disco, puedes repetir el paso manualmente:
	```sh
	python3 -m pip install --no-cache-dir --upgrade "aiomqtt>=2.4,<3.0" "paho-mqtt>=2.1,<3.0"
	```
	Vuelve a lanzar `./3_install.sh` cuando el comando termine con éxito.
- Para limpiar restos de IPKs antiguos, ejecuta `opkg remove python3-aiomqtt python3-paho-mqtt` antes de iniciar el instalador.

## Despliegue seguro

### 0. Credenciales compartidas (daemon, CGI y scripts)

> **Nota:** Todo el árbol sigue iniciando con el placeholder `changeme123` definido en la sección `yunbridge.general.serial_shared_secret` de UCI y en los ejemplos de la librería. Solo sirve para demos; `RuntimeConfig.__post_init__` lo rechaza, así que rota el material con `tools/rotate_credentials.sh` o desde LuCI y pega el snippet `#define BRIDGE_SERIAL_SHARED_SECRET "..."` en tu sketch antes de exponer el equipo. Consulta la guía completa en [`docs/CREDENTIALS.md`](docs/CREDENTIALS.md).

	```sh
	SECRET=$(openssl rand -hex 32)
	PASS=$(openssl rand -base64 24)
	uci batch <<EOF
	set yunbridge.general.serial_shared_secret='$SECRET'
	set yunbridge.general.mqtt_user='yunbridge-daemon'
	set yunbridge.general.mqtt_pass='$PASS'
	commit yunbridge
	EOF
	/etc/init.d/yunbridge restart
	```
- También puedes usar `tools/rotate_credentials.sh --host <yun>` o la pestaña *Credentials & TLS* (que imprime el snippet `#define BRIDGE_SERIAL_SHARED_SECRET`) para ejecutar ese procedimiento remotamente mediante `/usr/bin/yunbridge-rotate-credentials`, que ahora actualiza UCI directamente.
- `3_install.sh` reprovisiona por defecto `/etc/yunbridge/tls/ca.crt`, `yunbridge.crt` y `yunbridge.key` en cada ejecución, reescribiendo la ruta heredada (`/etc/ssl/certs/ca-certificates.crt`) y cualquier `cert/key` inexistente para apuntar al bundle privado bajo `/etc/yunbridge/tls`. El script instala automáticamente la dependencia `openssl-util` si falta antes de generar el material. Importa el CA resultante en tu broker si habilitarás TLS mutuo; exporta `YUNBRIDGE_FORCE_TLS_REGEN=0` (o `YUNBRIDGE_SKIP_TLS_AUTOGEN=1`) si prefieres mantener tus propios archivos y quieres que el instalador solo valide que existen.
### 1. Autenticación del enlace serie MCU ↔ Linux

- El handshake usa un tag HMAC-SHA256 (16 bytes) derivado de `serial_shared_secret`; si el secreto no existe o es débil, el daemon se niega a arrancar.
- Genera un secreto único por dispositivo (mínimo 8 bytes, idealmente 32) y aplícalo antes de iniciar el servicio:
	```sh
	openssl rand -hex 32 | awk '{print tolower($0)}' \
	  | uci set yunbridge.general.serial_shared_secret="$(cat)"
	uci commit yunbridge
	/etc/init.d/yunbridge restart
	```
- También puedes exportar `YUNBRIDGE_SERIAL_SECRET` en `/etc/rc.local` o en el `procd` `env` si prefieres inyectar el valor en runtime sin escribirlo en UCI.
- En el sketch define `#define BRIDGE_SERIAL_SHARED_SECRET "..."` (o usa el snippet que muestra LuCI) y vuelve a cargar el firmware; sin esto, el MCU rechazará el handshake. Los ejemplos de la librería incluyen por defecto `changeme123`, pero debes reemplazarlo antes de producción o correr `tools/rotate_credentials.sh` para regenerar ambos extremos.

### 2. Políticas de comando y topics sensibles

- `allowed_commands` controla los binarios que el daemon puede lanzar vía MQTT o el MCU. Un valor vacío significa *ningún comando permitido*; evita usar `*` salvo en laboratorios.
- Cada acción MQTT sensible ahora se puede permitir/denegar de forma granular con:
	- `mqtt_allow_file_read`, `mqtt_allow_file_write`, `mqtt_allow_file_remove`
	- `mqtt_allow_datastore_get`, `mqtt_allow_datastore_put`
	- `mqtt_allow_mailbox_read`, `mqtt_allow_mailbox_write`
	- `mqtt_allow_shell_run`, `mqtt_allow_shell_run_async`, `mqtt_allow_shell_poll`, `mqtt_allow_shell_kill`
- Configúralos en LuCI (sección **Services → YunBridge → Security**) o vía CLI:
	```sh
	uci set yunbridge.general.mqtt_allow_file_write='0'
	uci set yunbridge.general.mqtt_allow_mailbox_write='0'
	uci commit yunbridge && /etc/init.d/yunbridge reload
	```
- Cuando una acción está bloqueada, el daemon publica `bridge-error=topic-action-forbidden` en `br/system/status`, de modo que los consumidores reciben un fallo explícito.

### 3. Recomendaciones para ACLs MQTT

- Crea credenciales dedicadas para el daemon con permiso de publicar en `br/#` y subscribirse únicamente a los prefijos configurados.
- Limita a los clientes externos para que solo puedan `PUBLISH` en los topics necesarios (`br/d/+/mode`, `br/datastore/put/...`, etc.) y nunca en `system/status` o `sh/run_async`.
- En brokers tipo Mosquitto:
	```
	# /etc/mosquitto/acl
	user yunbridge-daemon
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
1.  **`openwrt-yun-bridge`**: El daemon principal de Python que se ejecuta en el MPU.
2.  **`openwrt-library-arduino`**: La librería C++ para el sketch que se ejecuta en el MCU.
3.  **`luci-app-yunbridge`**: La interfaz de configuración web.
4.  **`openwrt-yun-examples-python`**: Paquete cliente con ejemplos de uso.
5.  **`openwrt-yun-core`**: Ficheros de configuración base del sistema.

> ¿Buscas detalles adicionales sobre flujos internos, controles de seguridad y observabilidad? Revisa [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) para obtener un desglose actualizado.

> **Nota:** Todas las dependencias del daemon se instalan vía `opkg`, salvo las bibliotecas que solo existen en PyPI (`aiomqtt==2.4.0`, `paho-mqtt==2.1.0`, `pyserial-asyncio==0.6`, `cobs==1.2.2`, `tenacity==9.1.2`, `prometheus-client==0.23.1`, `persist-queue==1.1.0`, `attrs==24.2.0`). Esos paquetes se obtienen automáticamente de PyPI durante `3_install.sh`, que ahora lee `dependencies/runtime.toml` y aplica los mismos pines usados por `requirements/runtime.txt`. El resto (`python3-pyserial==3.5`, `python3-psutil==5.9.8`, `python3-more-itertools==10.8.0`, etc.) sigue llegando como IPKs tradicionales. El inventario completo vive en `dependencies/runtime.toml`; ejecuta `./tools/sync_runtime_deps.py` tras modificarlo para regenerar `requirements/runtime.txt`, refrescar el `Makefile` e instruir a `1_compile.sh`.

### Flujo reproducible con PyPI

- **Instala localmente los mismos paquetes que el daemon:**
	```sh
	python3 -m pip install --upgrade -r requirements/runtime.txt
	```
	Esto usa exactamente los pines publicados en `dependencies/runtime.toml`, por lo que cualquier entorno virtual replica al daemon.
- **Valida que los pines sigan disponibles en PyPI:** con `pip>=24.2` puedes hacer un chequeo sin modificar tu entorno usando `python3 -m pip install --dry-run -r requirements/runtime.txt`. En versiones anteriores de `pip`, ejecuta el mismo comando sin `--dry-run` dentro de un entorno temporal.
- **Dispositivos OpenWrt:** `3_install.sh` ahora genera una lista temporal a partir de `dependencies/runtime.toml` e instala exclusivamente los paquetes que NO tienen contraparte `opkg`, garantizando que el sistema embebido y tu entorno local comparten versiones.
- **Automatiza las revisiones:** añade `./tools/sync_runtime_deps.py --check` a tus pipelines para asegurar que ningún commit olvida actualizar los artefactos derivados.
- **Pruebas locales:** los entornos de `tox` consumen `requirements/runtime.txt`, así que `tox -e py311,py312` siempre valida contra el mismo conjunto de librerías que se usan en producción.

## Primeros Pasos

1.  **Compilar:** Ejecuta `./1_compile.sh` para preparar el SDK y compilar los paquetes IPK de OpenWRT.
2.  **Instalar:** Transfiere el proyecto a tu Yún y ejecuta `./3_install.sh` para instalar el software y las dependencias.
	- El script pedirá confirmación antes de lanzar `opkg upgrade`. Exporta `YUNBRIDGE_AUTO_UPGRADE=1` si necesitas ejecución no interactiva.
	> **Nota:** `3_install.sh` combina `opkg` (IPKs tradicionales) con una instalación controlada vía `pip3` para los paquetes que solo existen en PyPI. Si se interrumpe esa fase, verifica la conectividad TLS y ejecuta `python3 -m pip install --no-cache-dir --upgrade -r requirements/runtime.txt` antes de relanzar el instalador para asegurarte de que cada pin declarado en `dependencies/runtime.toml` quedó aplicado.
3.  **Configurar:** Accede a la interfaz web de LuCI en tu Yún, navega a `Services > YunBridge` y configura el daemon. Antes de ponerlo en producción usa la pestaña *Credentials & TLS* (o `tools/rotate_credentials.sh --host <yun>`) para rotar el secreto serie y las credenciales MQTT directamente en UCI.
4.  **Explorar:** Revisa los ejemplos en `openwrt-yun-examples-python/` para aprender a interactuar con el puente a través de MQTT.

### Verificación y control de calidad

- **Tipado estático:** Ejecuta `pyright` en la raíz del repositorio antes de enviar parches; la configuración (`pyrightconfig.json`) está preparada para ignorar los ejemplos legacy y validar el daemon y sus utilidades.
- **Cobertura Python:** Lanza `./tools/coverage_python.sh` (o simplemente `tox -e coverage`, que encadena ambos scripts) para generar `coverage/python/` con reportes `term-missing`, `coverage.xml` y HTML. Puedes pasar argumentos extra (por ejemplo un subconjunto de tests) y exportar `COVERAGE_ROOT` si necesitas otro directorio.
- **Cobertura C++:** Ejecuta `./tools/coverage_arduino.sh` o reutiliza el `tox -e coverage` anterior para compilar un harness host que prueba el protocolo binario con `g++ -fprofile-arcs -ftest-coverage`, ejecuta los tests y genera reportes en `coverage/arduino/`. Si ya tienes un build instrumentado diferente, exporta `BUILD_DIR`/`OUTPUT_ROOT` o `FORCE_REBUILD=1` para reutilizarlo.
- **Resumen automático:** Después de correr ambos scripts, ejecuta `python tools/coverage_report.py` para generar una tabla consolidada (`coverage/coverage-summary.md` + `.json`). El workflow de CI hace esto automáticamente y publica la tabla en el *GitHub Step Summary*, además de adjuntar el markdown como artefacto.
- **Artefactos de cobertura en CI:** El workflow `coverage` de GitHub Actions corre `tox -e coverage` en Python 3.11 y publica los directorios `coverage/python` y `coverage/arduino` como artefactos para cada PR/commit, facilitando su inspección sin levantar un entorno local.
- **Matriz Python 3.11/3.12:** Usa `tox` para ejecutar la suite completa en ambos intérpretes y detectar regresiones antes de desplegar:
	```sh
	tox -e py311,py312
	```
- **Smoke test remoto:** `./tools/hardware_smoke_test.sh --host <yun>` invoca `/usr/bin/yunbridge-hw-smoke` vía SSH y falla si el daemon no responde a `br/system/status` en menos de 7 segundos.
- **Harness multi-dispositivo:** `./tools/hardware_harness.py --manifest hardware/targets.toml --max-parallel 3 --tag regression` recorre todos los dispositivos definidos en el manifiesto, ejecuta el script anterior mediante SSH y al final resume qué nodos pasaron/ fallaron (además de producir un reporte JSON opcional).
- **Pruebas manuales:** Tras instalar los paquetes IPK en tu Yún, verifica el flujo end-to-end ejecutando uno de los scripts de `openwrt-yun-examples-python` y revisa el nuevo log del daemon (`/var/log/yunbridge.log`).
- **Diagnóstico en el MCU:** Carga el sketch `openwrt-library-arduino/examples/FrameDebug/FrameDebug.ino` para imprimir cada 5 s el snapshot de transmisión y confirmar que `expected_serial_bytes` coincide con `last_write_return`.
- **Monitoreo:** El daemon expone estados y errores del MCU en `br/system/status` (JSON) y publica el tamaño actual de la cola MQTT en `/tmp/yunbridge_status.json` junto al límite configurado. Ese snapshot ahora incluye `mqtt_spool_*` y `watchdog_*` (latido, intervalo, habilitado) para que LuCI los muestre sin parsers adicionales. Además, `br/system/metrics` adjunta las mismas claves y propiedades MQTT (`bridge-spool`, `bridge-watchdog-enabled`, `bridge-watchdog-interval`) para que los consumidores puedan alertar cuando el spool persistente se degrada o el watchdog deja de latir.
- **Telemetría reforzada:** `RuntimeState` cuenta los eventos de drop y truncamiento en todas las colas (MQTT, consola, mailbox y mailbox_incoming) y el writer periódico exporta los acumuladores en `/tmp/yunbridge_status.json` (`*_dropped_*`, `*_truncated_*`) junto con los tamaños actuales. Estos mismos contadores se publican en `br/system/status` para integrarse con dashboards MQTT.
- **Snapshots `br/system/bridge/*`:** ahora puedes consultar el estado del enlace serie sin inspeccionar archivos locales. Publica un mensaje vacío en `br/system/bridge/handshake/get` o `br/system/bridge/summary/get` (MQTT v5 opcionalmente con `response_topic`) y el daemon responderá con `.../handshake/value` o `.../summary/value` en JSON (`content-type: application/json`). Incluye sincronización actual, contadores de handshake, versión del MCU, pipeline serial en curso y el último comando completado, además de adjuntar la propiedad de usuario `bridge-snapshot` para que los clientes puedan enrutar la respuesta.
- **Exportador Prometheus:** Habilita `uci set yunbridge.general.metrics_enabled='1'` (o exporta `YUNBRIDGE_METRICS_ENABLED=1`) para exponer `http://<host>:<metrics_port>/metrics` con `Content-Type: text/plain; version=0.0.4`. Ajusta `metrics_host`/`metrics_port` si necesitas escuchar en otra interfaz. Verifica con `curl http://127.0.0.1:9130/metrics` y busca gauges como `yunbridge_mqtt_queue_size` o `yunbridge_serial_decode_errors`. Los campos de texto se representan como `yunbridge_info{key="...",value="..."} 1`.