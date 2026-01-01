# YunBridge Architecture

Esta nota resume cómo se articula el daemon, qué garantías de seguridad ofrece y cómo se observan los flujos críticos tras la modernización de noviembre 2025.

## Componentes

- **BridgeService (Python)**: orquesta la comunicación MCU↔Linux, aplica las políticas de topics y delega la ejecución a los componentes (`FileComponent`, `ProcessComponent`, etc.).
- **RuntimeState**: almacena el estado mutable del daemon (colas MQTT, handshake, spool, métricas) y expone snapshots consistentes para el status writer, MQTT y el exportador de Prometheus.
- **MQTT Publisher**: publica respuestas y telemetría usando MQTT v5 (propiedades de correlación, expiración y metadatos) para mantener la compatibilidad con clientes modernos.
- **MCU Firmware (openwrt-library-arduino)**: implementa el protocolo binario descrito en `tools/protocol/spec.toml` y vela por el secreto compartido del enlace serie.
- **Instrumentación**: el daemon escribe `/tmp/yunbridge_status.json`, publica métricas en `br/system/metrics` y, a partir de esta versión, mantiene un exportador HTTP opcional compatible con Prometheus.

## Arquitectura de la Librería Arduino (C++)

En la versión 2.0, la librería Arduino ha sido refactorizada para eliminar el patrón "God Object" de la clase `Bridge`.

- **Desacoplamiento**: La lógica de negocio se ha movido de `Bridge.cpp` a clases especializadas: `DataStoreClass`, `ProcessClass`, `MailboxClass`, `FileSystemClass` y `ConsoleClass`.
- **Enrutamiento**: La clase `Bridge` actúa ahora principalmente como un router. Su método `dispatch()` recibe los frames del puerto serie y los deriva a la instancia del componente correspondiente (por ejemplo, `DataStore.handleResponse()`).
- **Gestión de Estado**: Cada componente gestiona su propio estado interno (colas de procesos, buffers, etc.), lo que facilita las pruebas unitarias y el mantenimiento.
- **Compatibilidad**: Se mantiene la API pública original (`Bridge.put()`, `Process.run()`, etc.) para garantizar la compatibilidad con los sketches existentes, pero internamente estas llamadas delegan en los nuevos componentes.

## Seguridad

1. **TLS recomendado**: por defecto `mqtt_tls=1` y se exige `mqtt_cafile` para levantar el contexto TLS. Puedes desactivar TLS explícitamente (por ejemplo desde LuCI) para entornos de depuración, pero el daemon lo registra como advertencia y todo el tráfico MQTT —incluyendo credenciales— viaja en texto plano.
	2. **Secreto serie fuerte**: el handshake MCU↔Linux exige un `serial_shared_secret` de al menos ocho bytes con cuatro símbolos distintos. El demonio genera un nonce de 16 bytes y valida `HMAC-SHA256(secret, nonce)` truncado a 16 bytes. Los chequeos viven en `RuntimeConfig.__post_init__` para evitar estados inseguros. El sistema provisiona un secreto único por dispositivo (via `uci-defaults`/instalador) si falta, y el daemon rechaza explícitamente el placeholder histórico (`changeme123`) para evitar despliegues inseguros.
	Además, `tests/test_protocol_contract.py` incluye pruebas que fijan el layout binario `>HBI` y un vector de referencia HMAC para detectar inmediatamente cualquier deriva entre Python y la librería Arduino.
3. **Lista blanca de comandos**: `allowed_commands` se normaliza en `AllowedCommandPolicy` y se vuelve a aplicar en `ProcessComponent` y `ShellComponent` mediante el sanitizador compartido de `yunbridge.policy`.
4. **Topics sensibles**: `TopicAuthorization` gobierna los toggles `mqtt_allow_*` para archivos, datastore, mailbox, shell, consola y pines digitales/analógicos (`mqtt_allow_console_input`, `mqtt_allow_digital_write`, etc.). Cualquier combinación topic/acción no declarada queda denegada automáticamente, así que los nuevos topics requieren añadir un flag explícito antes de exponerse.
5. **Sandbox de archivos**: `FileComponent` normaliza las rutas con `PurePosixPath`, evita saltos (`..`) y obliga a permanecer bajo `file_system_root`. Además, aplica los límites configurables `file_write_max_bytes` (tamaño máximo por frame) y `file_storage_quota_bytes` (cuota total bajo el sandbox). Si se superan, devuelve `write_limit_exceeded` o `storage_quota_exceeded`, incrementa los contadores en `RuntimeState` y registra la causa en los snapshots para que LuCI/Prometheus puedan alertar.

## Observabilidad

- **Logging estructurado**: todo el árbol `yunbridge.*` escribe líneas JSON (`ts`, `level`, `logger`, `message`, `extra`). Esto facilita enviar los logs directamente a syslog, Loki o Elastic sin parsers adicionales.
- **Metrics MQTT**: `publish_metrics()` sigue publicando snapshots periódicos en `br/system/metrics` con la misma estructura JSON usada por `RuntimeState`.
- **Exportador Prometheus** *(nuevo)*: al habilitar `metrics_enabled`, el daemon levanta un listener HTTP (por defecto `127.0.0.1:9130`) respaldado por `prometheus_client`. Expone todas las métricas numéricas en el formato `CONTENT_TYPE_LATEST` y los campos no numéricos se representan como `yunbridge_info{key="...",value="..."} 1`.
- Los snapshots publicados (prometheus, status JSON y MQTT) incluyen `mqtt_spool_*` y `watchdog_*`, y además `br/system/metrics` adjunta propiedades MQTT `bridge-spool`, `bridge-watchdog-enabled` e `bridge-watchdog-interval` para que la UI o los brokers puedan alertar sin parsear JSON.
- **Snapshot del enlace (`br/system/bridge/*`)**: cualquier cliente puede pedir `br/system/bridge/handshake/get` o `br/system/bridge/summary/get` y recibirá un JSON con el estado del handshake, la versión del MCU, el pipeline serial (comando en vuelo y último resultado) y el flujo de métricas del enlace. Es la misma estructura que ahora aparece embebida en `/tmp/yunbridge_status.json`, `br/system/status` y el exportador Prometheus bajo la clave `bridge`.
- **Status Writer**: `status_writer()` mantiene `/tmp/yunbridge_status.json` como snapshot local para depuración rápida y para scripts de LuCI.
- **Cuotas de archivos visibles**: `RuntimeState` añade `file_storage_bytes_used`, `file_write_limit_rejections` y `file_storage_limit_rejections` en cada snapshot (`/tmp/yunbridge_status.json`, `br/system/status`, `br/system/metrics`) para monitorear cuánta cuota resta y cuántas operaciones se bloquearon por superar los límites.

## Configuración relevante

| Clave | Descripción | Valor por defecto |
| --- | --- | --- |
| `metrics_enabled` | Activa el exportador Prometheus embebido. | `0` (deshabilitado) |
| `metrics_host` | Dirección de enlace para el exportador. | `127.0.0.1` |
| `metrics_port` | Puerto TCP del exportador/text format. | `9130` |
| `debug_logging` | Fuerza nivel `DEBUG` en los logs JSON. | `0` |
| `allowed_commands` | Lista blanca de comandos shell. | `""` (ninguno) |
| `file_write_max_bytes` | Máximo de bytes aceptados por cada operación `CMD_FILE_WRITE`/MQTT write. | `262144` (256 KiB) |
| `file_storage_quota_bytes` | Cuota global dentro de `file_system_root`; rechaza nuevas escrituras al superarse. | `4194304` (4 MiB) |

Puedes definirlas vía UCI (`uci set yunbridge.general.metrics_enabled='1'`) o con variables de entorno `YUNBRIDGE_METRICS_*` antes de iniciar el servicio (`procd`/`systemd`).

## Flujo de inicio (resumen)

1. `main()` carga la configuración (`load_runtime_config()`), provisiona logging estructurado y crea `RuntimeState`.
2. Se arranca un `TaskGroup` con: lector serie, cliente MQTT, escritor de estado, publicador de métricas MQTT, watchdog opcional y exportador Prometheus (si está habilitado).
3. Cada tarea informa eventos mediante logs JSON; las fallas críticas se elevan como `CRITICAL` para forzar reinicios supervisados por `procd`.
4. Al cerrar, el daemon limpia `/tmp/yunbridge_status.json`, detiene el exportador y deja que `procd`/systemd relance según política.

Consulta también `README.md` (sección *Monitoreo*) para instrucciones de despliegue y los scripts en `tools/` para automatizar smoke tests o rotación de credenciales.
