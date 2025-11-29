# YunBridge Architecture

Esta nota resume cómo se articula el daemon, qué garantías de seguridad ofrece y cómo se observan los flujos críticos tras la modernización de noviembre 2025.

## Componentes

- **BridgeService (Python)**: orquesta la comunicación MCU↔Linux, aplica las políticas de topics y delega la ejecución a los componentes (`FileComponent`, `ProcessComponent`, etc.).
- **RuntimeState**: almacena el estado mutable del daemon (colas MQTT, handshake, spool, métricas) y expone snapshots consistentes para el status writer, MQTT y el exportador de Prometheus.
- **MQTT Publisher**: publica respuestas y telemetría usando MQTT v5 (propiedades de correlación, expiración y metadatos) para mantener la compatibilidad con clientes modernos.
- **MCU Firmware (openwrt-library-arduino)**: implementa el protocolo binario descrito en `tools/protocol/spec.toml` y vela por el secreto compartido del enlace serie.
- **Instrumentación**: el daemon escribe `/tmp/yunbridge_status.json`, publica métricas en `br/system/metrics` y, a partir de esta versión, mantiene un exportador HTTP opcional compatible con Prometheus.

## Seguridad

1. **TLS obligatorio**: `mqtt_tls=1` y la presencia de `mqtt_cafile` son requisitos al cargar la configuración; el daemon se niega a arrancar si faltan.
2. **Secreto serie fuerte**: el handshake MCU↔Linux exige un `serial_shared_secret` de al menos ocho bytes con cuatro símbolos distintos. Los chequeos viven en `RuntimeConfig.__post_init__` para evitar estados inseguros.
3. **Lista blanca de comandos**: `allowed_commands` se normaliza en `AllowedCommandPolicy` y se vuelve a aplicar en `ProcessComponent` y `ShellComponent` mediante el sanitizador compartido de `yunbridge.policy`.
4. **Topics sensibles**: `TopicAuthorization` permite deshabilitar acciones MQTT específicas (`mqtt_allow_file_write`, `mqtt_allow_mailbox_write`, etc.) sin recompilar.
5. **Sandbox de archivos**: `FileComponent` normaliza las rutas con `PurePosixPath`, evita saltos (`..`) y obliga a permanecer bajo `file_system_root`.

## Observabilidad

- **Logging estructurado**: todo el árbol `yunbridge.*` escribe líneas JSON (`ts`, `level`, `logger`, `message`, `extra`). Esto facilita enviar los logs directamente a syslog, Loki o Elastic sin parsers adicionales.
- **Metrics MQTT**: `publish_metrics()` sigue publicando snapshots periódicos en `br/system/metrics` con la misma estructura JSON usada por `RuntimeState`.
- **Exportador Prometheus** *(nuevo)*: al habilitar `metrics_enabled`, el daemon levanta un listener HTTP (por defecto `127.0.0.1:9130`) respaldado por `prometheus_client`. Expone todas las métricas numéricas en el formato `CONTENT_TYPE_LATEST` y los campos no numéricos se representan como `yunbridge_info{key="...",value="..."} 1`.
- **Status Writer**: `status_writer()` mantiene `/tmp/yunbridge_status.json` como snapshot local para depuración rápida y para scripts de LuCI.

## Configuración relevante

| Clave | Descripción | Valor por defecto |
| --- | --- | --- |
| `metrics_enabled` | Activa el exportador Prometheus embebido. | `0` (deshabilitado) |
| `metrics_host` | Dirección de enlace para el exportador. | `127.0.0.1` |
| `metrics_port` | Puerto TCP del exportador/text format. | `9130` |
| `debug_logging` | Fuerza nivel `DEBUG` en los logs JSON. | `0` |
| `allowed_commands` | Lista blanca de comandos shell. | `""` (ninguno) |

Puedes definirlas vía UCI (`uci set yunbridge.general.metrics_enabled='1'`) o con variables de entorno `YUNBRIDGE_METRICS_*` antes de iniciar el servicio (`procd`/`systemd`).

## Flujo de inicio (resumen)

1. `main()` carga la configuración (`load_runtime_config()`), provisiona logging estructurado y crea `RuntimeState`.
2. Se arranca un `TaskGroup` con: lector serie, cliente MQTT, escritor de estado, publicador de métricas MQTT, watchdog opcional y exportador Prometheus (si está habilitado).
3. Cada tarea informa eventos mediante logs JSON; las fallas críticas se elevan como `CRITICAL` para forzar reinicios supervisados por `procd`.
4. Al cerrar, el daemon limpia `/tmp/yunbridge_status.json`, detiene el exportador y deja que `procd`/systemd relance según política.

Consulta también `README.md` (sección *Monitoreo*) para instrucciones de despliegue y los scripts en `tools/` para automatizar smoke tests o rotación de credenciales.
