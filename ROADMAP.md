# Arduino Yun v2 Ecosystem Roadmap

## Estado global

- ✅ Dependencias Python migradas a paquetes mantenidos (`python3-paho-mqtt`, `python3-pyserial`) y a IPK construidos desde PyPI (`python3-pyserial-asyncio`, `python3-cobs`), eliminando módulos vendorizados y la necesidad de `pip` en OpenWrt.
- ✅ Documentación de protocolo, instalación y QA actualizada (ver `README.md` y `openwrt-library-arduino/docs/PROTOCOL.md`).
- ✅ Procesos asíncronos con buffering persistente y publicación de flags de truncamiento en MQTT.
- ⏳ Automatizar pruebas end-to-end sobre hardware real.

## Prioridades 2026

### MQTT y mensajería
- Certificate support (cliente y broker) con despliegue guiado desde LuCI.
- Soporte opcional de WebSockets para clientes externos.
- Reglas avanzadas de autorización por tópico.

### Comunicación MCU ↔️ MPU
- Documentar y versionar el protocolo en un paquete independiente.
- ✅ Implementar reintentos segmentados para payloads mayores a 256 bytes (Bridge.cpp re-pregunta automáticamente hasta vaciar stdout/stderr en polls sucesivos).

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
