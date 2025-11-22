# Arduino Yun v2 Ecosystem Roadmap

## Estado global

- ✅ Protocolo RPC centralizado en `tools/protocol/spec.toml` con generador que sincroniza bindings C++/Python.
- ✅ Regeneración de `rpc_protocol.h` y `yunbridge/rpc/protocol.py` + validación manual vía `console_test.py`, `led13_test.py` y `datastore_test.py`.
- ✅ Logging del daemon enriquecido para distinguir fallos de COBS vs parsing de frame.
- ✅ Instrumentación MCU (`BRIDGE_DEBUG_FRAMES`) para snapshots de longitudes/raw/CRC enviados.
- ✅ Watchdog keepalive alineado entre `yunbridge.init` (procd) y el daemon Python.
- ✅ Plan de compatibilidad documentado: daemon basado en Python 3.11 con test matrix en 3.12 y compilaciones OpenWrt/AVR sobre GCC 13 (SDK 24.10.4 + validación cruzada en 23.05).
- ✅ Secretos centralizados: `/etc/yunbridge/credentials` alimenta a procd/envfile y reemplaza los valores sensibles en UCI.
- ✅ Tooling/Scripts: rotación de credenciales (`yunbridge-rotate-credentials`, `tools/rotate_credentials.sh`) y smoke-tests automatizados (`yunbridge-hw-smoke`, LuCI *Credentials & TLS*).
- ⏳ Automatizar pruebas end-to-end sobre hardware real.

## Prioridades 2026

### MQTT y mensajería
- Certificate support (cliente y broker) con despliegue guiado desde LuCI.
- Soporte opcional de WebSockets para clientes externos.
- Reglas avanzadas de autorización por tópico.
- Endurecer el spool MQTT: detección temprana de corrupción o disco lleno + alertas y caída controlada al modo sin persistencia.

### Comunicación MCU ↔️ MPU
- Investigar pérdidas parciales en frames MCU→Linux (errores COBS decode) aprovechando las nuevas métricas `Bridge.getTxDebugSnapshot()`.
- Documentar y versionar el protocolo en un paquete independiente.
- Añadir backoff exponencial y alerta administrativa cuando se repite `sync_auth_mismatch` para evitar loops de handshake.

### Procesos y shell
- Blindar `ProcessComponent` para que limpie `running_processes` si `asyncio.create_subprocess_exec` falla y así evitar zombies/entradas huérfanas.

### Core Yun / OpenWRT
- Compilar y empaquetar `python3-sqlite3` dentro del feed para garantizar que el spool SQLite funcione en imágenes personalizadas.
- Añadir targets recientes (ex. ramips/mt7621) a la canalización de CI.
- Generar imágenes de firmware preconfiguradas para demo o labs educativos.
- Consola serie dedicada para debug (evitar reutilizar ttyATH0 del bridge).

### Modernización del daemon Python
- Adoptar pattern matching estructural y `contextlib.AsyncExitStack` en los componentes (file, mailbox, process, shell) para simplificar operaciones con estado.
- Reemplazar `schedule_background` por grupos de tareas supervisados que propaguen cancelaciones y reporten fallos con contexto.
- Extraer constantes y utilidades del protocolo a un módulo dedicado compartido por todos los componentes para reducir el uso de `from ..const import ...`.
- Investigar el uso de `pydantic` o `attrs` para validar payloads MQTT entrantes y eliminar validaciones ad-hoc.

### Web UI (luci-app-yunbridge)
- Dashboard en vivo basado 100% en MQTT.
- Editor de reglas simples (GPIO ↔ procesos) desde el navegador.
- Localización ampliada (FR/DE) y tema responsivo.

---

¡Las contribuciones y sugerencias son bienvenidas! Abre un issue o PR con tus ideas.
