# Arduino Yun v2 Ecosystem Roadmap

## Estado global

- [ ] Automatizar pruebas end-to-end sobre hardware real.

## Prioridades 2026

### MQTT y mensajería
- [ ] Endurecer el spool MQTT: detección temprana de corrupción o disco lleno + alertas y caída controlada al modo sin persistencia.

### Comunicación MCU ↔️ MPU
- [ ] Investigar pérdidas parciales en frames MCU→Linux (errores COBS decode) aprovechando las nuevas métricas `Bridge.getTxDebugSnapshot()`.

### Core Yun / OpenWRT
- [ ] Añadir targets recientes (ex. ramips/mt7621) a la canalización de CI.
- [ ] Generar imágenes de firmware preconfiguradas para demo o labs educativos.
- [ ] Consola serie dedicada para debug (evitar reutilizar ttyATH0 del bridge).

### Modernización del daemon Python
- [ ] Adoptar pattern matching estructural y `contextlib.AsyncExitStack` en los componentes (file, mailbox, process, shell) para simplificar operaciones con estado.
- [ ] Investigar el uso de `pydantic` o `attrs` para validar payloads MQTT entrantes y eliminar validaciones ad-hoc.

### Web UI (luci-app-yunbridge)
- [ ] Dashboard en vivo basado 100% en MQTT.
- [ ] Editor de reglas simples (GPIO ↔ procesos) desde el navegador.
- [ ] Localización ampliada (FR/DE) y tema responsivo.

## Resumen de estado

| Área | Iniciativa | Estado | Notas |
| --- | --- | --- | --- |
| Estado global | Automatizar pruebas end-to-end sobre hardware real | Pendiente | Necesita harness contra hardware físico (scripts en `tools/hardware_smoke_test.sh` sólo cubren smoke tests). |
| MQTT y mensajería | Endurecer el spool MQTT | Pendiente | Falta detección temprana de corrupción/disco lleno y fallback sin persistencia en `yunbridge/mqtt/spool.py`. |
| MCU ↔️ MPU | Investigar pérdidas parciales (COBS) | Pendiente | Requiere análisis con `Bridge.getTxDebugSnapshot()` y telemetría adicional en la librería AVR. |
| Core Yun / OpenWRT | Añadir targets recientes a la CI | Pendiente | Workflow `.github/workflows/ci.yml` sólo cubre Python; no prueba nuevos targets como ramips/mt7621. |
| Core Yun / OpenWRT | Imágenes preconfiguradas para demos | Pendiente | No existen recetas/fotos en `openwrt-sdk/` para snapshots listos. |
| Core Yun / OpenWRT | Consola serie dedicada | Pendiente | Aún se comparte `ttyATH0`; no hay servicio paralelo de depuración. |
| Modernización daemon | Adoptar pattern matching + `AsyncExitStack` | Pendiente | Componentes `file`, `mailbox`, `process`, `shell` siguen usando `if/elif` y contextos manuales. |
| Modernización daemon | Validar payloads con `pydantic`/`attrs` | Pendiente | Validaciones continúan ad-hoc en handlers MQTT. |
| Web UI | Dashboard en vivo 100% MQTT | Pendiente | Sólo existe demo `root/www/yunbridge/index.html`. |
| Web UI | Editor de reglas (GPIO ↔ procesos) | Pendiente | No hay interfaz para generar reglas desde navegador. |
| Web UI | Localización FR/DE + tema responsivo | Pendiente | Traducciones limitadas a ES/EN y UI bootstrap básica. |

---

¡Las contribuciones y sugerencias son bienvenidas! Abre un issue o PR con tus ideas.
