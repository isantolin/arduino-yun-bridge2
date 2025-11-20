# Arduino Yún Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**Yún Bridge 2 es un reemplazo moderno, robusto y de alto rendimiento para el sistema Bridge original de Arduino Yún.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en el Yún, reemplazando la antigua solución basada en `python-bridge` por un daemon asíncrono y un protocolo RPC binario eficiente.

## Características Principales

- **Límites configurables:** Los buffers interno de consola y mailbox se pueden ajustar vía UCI (`console_queue_limit_bytes`, `mailbox_queue_limit`, `mailbox_queue_bytes_limit`) para prevenir desbordes en escenarios con alto tráfico.
	- Todos los valores por defecto consumidos por el daemon viven ahora en `openwrt-yun-bridge/yunbridge/const.py` (`DEFAULT_MQTT_PORT`, `DEFAULT_SERIAL_RETRY_TIMEOUT`, etc.), lo que evita duplicar literales entre módulos y pruebas.
- **Backpressure en MQTT con MQTT v5:** El tamaño de la cola de publicación hacia el broker se controla con `mqtt_queue_limit`, mientras que las conexiones salientes usan propiedades MQTT v5 (session expiry = 0, request/response info) para que los clientes sepan cuándo reiniciar suscripciones y puedan negociar flujos de respuesta.
- **Respuestas correladas en MQTT:** Cada publicación originada por el daemon puede reutilizar el `response_topic` proporcionado por el cliente y propaga un `correlation_data` binario, de modo que las respuestas (GPIO, datastore, mailbox, sistema, procesos, etc.) se pueden asociar de manera inequívoca incluso si varios consumidores comparten el mismo prefijo. Las propiedades de usuario (`bridge-request-topic`, `bridge-pin`, `bridge-datastore-key`, `bridge-file-path`, `bridge-process-pid`, `bridge-status`, …) transportan metadatos humanamente legibles sin romper la compatibilidad con clientes MQTT v3.
- **Handshake automático MCU ↔ Linux:** Tras cada reconexión, el daemon solicita `CMD_GET_VERSION` y publica la versión del firmware del sketch en `br/system/version/value`, de modo que los clientes pueden validar compatibilidad antes de ejecutar comandos.
- **Procesos asíncronos robustos:** Los polls sucesivos ahora entregan todo el `stdout`/`stderr` generado, incluso cuando los procesos producen más datos que un frame. El daemon mantiene buffers circulares por PID y conserva el `exit_code` hasta que el MCU confirma la lectura completa, mientras que la librería Arduino reenvía automáticamente `CMD_PROCESS_POLL` cuando recibe fragmentos parciales.
- **Estado inmediato de buzón:** Los sketches pueden invocar `Mailbox.requestAvailable()` y recibir el conteo pendiente en `Bridge.onMailboxAvailableResponse`, lo que evita lecturas vacías y mantiene sincronizado al MCU con la cola de Linux.

### Novedades (noviembre 2025)

- Especificación única del protocolo en `tools/protocol/spec.toml` con generador (`tools/protocol/generate.py`) que emite `openwrt-yun-bridge/yunbridge/rpc/protocol.py` y `openwrt-library-arduino/src/protocol/rpc_protocol.h`, garantizando consistencia MCU↔MPU.
- Migración del stack MQTT a **aiomqtt 2.4** + `paho-mqtt` 2.1: el daemon y los ejemplos usan un shim asíncrono compatible con la API previa de `asyncio-mqtt`, con soporte completo de MQTT v5 (propiedades de respuesta, clean start first-only, códigos de motivo enriquecidos) y reconexiones más predecibles en brokers modernos.
- **Correlación automática de peticiones MQTT v5:** todas las respuestas generadas por el daemon reutilizan `response_topic` y `correlation_data` cuando el cliente lo solicita. Además, se adjuntan propiedades de usuario (`bridge-request-topic`, `bridge-pin`, `bridge-datastore-key`, `bridge-file-path`, `bridge-process-pid`, etc.) y `message_expiry_interval` específicos por servicio para que los consumidores puedan validar el contexto original incluso cuando los mensajes pasan por brokers compartidos.
- Revisión manual de los bindings regenerados ejecutando `console_test.py`, `led13_test.py` y `datastore_test.py` del paquete `openwrt-yun-examples-python`, confirmando compatibilidad funcional.
- Instrumentación de logging en `bridge_daemon.py` para diferenciar errores de COBS decode de fallos al parsear frames, facilitando el diagnóstico de problemas en serie.
- **Telemetría de colas consolidada:** `RuntimeState` ahora registra métricas de drop/truncamiento por servicio (`mqtt_dropped_messages`, `console_dropped_chunks`, `mailbox_truncated_bytes`, etc.) y el writer periódico (`status_writer`) las expone tanto en `/tmp/yunbridge_status.json` como en los tópicos `br/system/status`, permitiendo integrar alertas en grafana/Prometheus sin parsers adicionales.
- El daemon ahora **falla en seguro** cuando `mqtt_tls=1`: si falta el CA o el certificado cliente, el arranque se aborta con error explícito.
- La ejecución remota de comandos MQTT requiere una lista blanca explícita (`yunbridge.general.allowed_commands`). Un valor vacío significa *ningún comando permitido*; use `*` para habilitar todos de forma consciente.
- **Keepalive de watchdog integrado:** cuando `procd` expone `PROCD_WATCHDOG` o exportas `YUNBRIDGE_WATCHDOG_INTERVAL`, el daemon lanza un `WatchdogKeepalive` asíncrono que pulsa `WATCHDOG=trigger` en `stdout` a intervalos seguros y reporta los latidos en `RuntimeState`. Ajusta `procd_set_param watchdog` en `yunbridge.init` si necesitas ventanas más estrictas.
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
- **Control explícito del flujo serie:** cada comando MCU se envía de uno en uno y se reintenta automáticamente si no llega `ACK` o la respuesta esperada. Ajusta `serial_retry_timeout` (segundos), `serial_response_timeout` y `serial_retry_attempts` para equilibrar latencia y resiliencia.
	- El instalador (`3_install.sh`) inicializa estos valores si aún no existen; personalízalos antes de ejecutar el daemon exportando `YUNBRIDGE_SERIAL_RETRY_TIMEOUT` o `YUNBRIDGE_SERIAL_RETRY_ATTEMPTS`.
- La librería Arduino (con `BRIDGE_DEBUG_FRAMES` activado) ahora mantiene estadísticas de transmisión (`Bridge.getTxDebugSnapshot()`, `Bridge.resetTxDebugStats()`), incluyendo tamaños raw/COBS, CRC y diferencias entre bytes esperados y escritos en serie, lo que ayuda a detectar truncamientos.
- Se mantiene la alineación del protocolo binario con la librería Arduino (prefijos de longitud y códigos de estado consistentes en datastore, mailbox y filesystem).
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

> **Nota:** Todas las dependencias del daemon se instalan vía `opkg`. `python3-aiomqtt` y `python3-paho-mqtt` traen el stack MQTT v5 actualizado, mientras que `python3-pyserial` llega desde los feeds oficiales de OpenWrt. `python3-pyserial-asyncio` y `python3-cobs` se empaquetan desde PyPI dentro de este repositorio y se distribuyen como `.ipk` junto al daemon.

## Primeros Pasos

1.  **Compilar:** Ejecuta `./1_compile.sh` para preparar el SDK y compilar los paquetes IPK de OpenWRT.
2.  **Instalar:** Transfiere el proyecto a tu Yún y ejecuta `./3_install.sh` para instalar el software y las dependencias.
	- El script pedirá confirmación antes de lanzar `opkg upgrade`. Exporta `YUNBRIDGE_AUTO_UPGRADE=1` si necesitas ejecución no interactiva.
3.  **Configurar:** Accede a la interfaz web de LuCI en tu Yún, navega a `Services > YunBridge` y configura el daemon.
4.  **Explorar:** Revisa los ejemplos en `openwrt-yun-examples-python/` para aprender a interactuar con el puente a través de MQTT.

### Verificación y control de calidad

- **Tipado estático:** Ejecuta `pyright` en la raíz del repositorio antes de enviar parches; la configuración (`pyrightconfig.json`) está preparada para ignorar los ejemplos legacy y validar el daemon y sus utilidades.
- **Cobertura Python:** Lanza `./tools/coverage_python.sh` para generar `coverage/python/` con reportes `term-missing`, `coverage.xml` y HTML. Puedes pasar argumentos extra (por ejemplo un subconjunto de tests) y exportar `COVERAGE_ROOT` si necesitas otro directorio.
- **Cobertura C++:** Ejecuta `./tools/coverage_arduino.sh` para compilar un harness host que prueba el protocolo binario con `g++ -fprofile-arcs -ftest-coverage`, ejecuta los tests y genera reportes en `coverage/arduino/`. Si ya tienes un build instrumentado diferente, exporta `BUILD_DIR`/`OUTPUT_ROOT` o `FORCE_REBUILD=1` para reutilizarlo.
- **Pruebas manuales:** Tras instalar los paquetes IPK en tu Yún, verifica el flujo end-to-end ejecutando uno de los scripts de `openwrt-yun-examples-python` y revisa el nuevo log del daemon (`/var/log/yunbridge.log`).
- **Diagnóstico en el MCU:** Carga el sketch `openwrt-library-arduino/examples/FrameDebug/FrameDebug.ino` para imprimir cada 5 s el snapshot de transmisión y confirmar que `expected_serial_bytes` coincide con `last_write_return`.
- **Monitoreo:** El daemon expone estados y errores del MCU en `br/system/status` (JSON) y publica el tamaño actual de la cola MQTT en `/tmp/yunbridge_status.json` junto al límite configurado.
 - **Telemetría reforzada:** `RuntimeState` cuenta los eventos de drop y truncamiento en todas las colas (MQTT, consola, mailbox y mailbox_incoming) y el writer periódico exporta los acumuladores en `/tmp/yunbridge_status.json` (`*_dropped_*`, `*_truncated_*`) junto con los tamaños actuales. Estos mismos contadores se publican en `br/system/status` para integrarse con dashboards MQTT.