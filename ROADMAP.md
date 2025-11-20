# Arduino Yun v2 Ecosystem Roadmap

## Estado global

- ✅ Protocolo RPC centralizado en `tools/protocol/spec.toml` con generador que sincroniza bindings C++/Python.
- ✅ Regeneración de `rpc_protocol.h` y `yunbridge/rpc/protocol.py` + validación manual vía `console_test.py`, `led13_test.py` y `datastore_test.py`.
- ✅ Logging del daemon enriquecido para distinguir fallos de COBS vs parsing de frame.
- ✅ Instrumentación MCU (`BRIDGE_DEBUG_FRAMES`) para snapshots de longitudes/raw/CRC enviados.
- ⏳ Automatizar pruebas end-to-end sobre hardware real.

## Prioridades 2026

### MQTT y mensajería
- Certificate support (cliente y broker) con despliegue guiado desde LuCI.
- Soporte opcional de WebSockets para clientes externos.
- Reglas avanzadas de autorización por tópico.

### Comunicación MCU ↔️ MPU
- Investigar pérdidas parciales en frames MCU→Linux (errores COBS decode) aprovechando las nuevas métricas `Bridge.getTxDebugSnapshot()`.
- Documentar y versionar el protocolo en un paquete independiente.

### Core Yun / OpenWRT
- Añadir targets recientes (ex. ramips/mt7621) a la canalización de CI.
- Generar imágenes de firmware preconfiguradas para demo o labs educativos.
- Consola serie dedicada para debug (evitar reutilizar ttyATH0 del bridge).

### Web UI (luci-app-yunbridge)
- Dashboard en vivo basado 100% en MQTT.
- Editor de reglas simples (GPIO ↔ procesos) desde el navegador.
- Localización ampliada (FR/DE) y tema responsivo.

---

¡Las contribuciones y sugerencias son bienvenidas! Abre un issue o PR con tus ideas.
