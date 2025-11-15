# Arduino Yún Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**Yún Bridge 2 es un reemplazo moderno, robusto y de alto rendimiento para el sistema Bridge original de Arduino Yún.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en el Yún, reemplazando la antigua solución basada en `python-bridge` por un daemon asíncrono y un protocolo RPC binario eficiente.

## Características Principales

- **Límites configurables:** Los buffers interno de consola y mailbox se pueden ajustar vía UCI (`console_queue_limit_bytes`, `mailbox_queue_limit`, `mailbox_queue_bytes_limit`) para prevenir desbordes en escenarios con alto tráfico.
- **Backpressure en MQTT:** El tamaño de la cola de publicación hacia el broker se controla con `mqtt_queue_limit`, evitando consumos de memoria descontrolados cuando el broker no está disponible.
- **Handshake automático MCU ↔ Linux:** Tras cada reconexión, el daemon solicita `CMD_GET_VERSION` y publica la versión del firmware del sketch en `br/system/version/value`, de modo que los clientes pueden validar compatibilidad antes de ejecutar comandos.
- **Procesos asíncronos robustos:** Los polls sucesivos ahora entregan todo el `stdout`/`stderr` generado, incluso cuando los procesos producen más datos que un frame. El daemon mantiene buffers circulares por PID y conserva el `exit_code` hasta que el MCU confirma la lectura completa, mientras que la librería Arduino reenvía automáticamente `CMD_PROCESS_POLL` cuando recibe fragmentos parciales.

### Novedades (noviembre 2025)

- Alineación total del protocolo binario con la librería de Arduino (prefijos de longitud y códigos de estado consistentes en datastore, mailbox y filesystem).
- Nuevo sistema de buffering persistente para `CMD_PROCESS_POLL_RESP`, evitando pérdidas cuando el proceso supera `MAX_PAYLOAD_SIZE` en una sola lectura.
- Se añadieron colas de estado en `RuntimeState` para reportar con precisión la finalización de procesos y los flags de truncamiento vía MQTT.

## Arquitectura

- **Callbacks de estado:** Registra `Bridge.onStatus(...)` en tus sketches para recibir `STATUS_*` desde Linux, incluyendo mensajes de error descriptivos cuando una operación (p.ej. I/O de archivos) falla.
1.  **`openwrt-yun-bridge`**: El daemon principal de Python que se ejecuta en el MPU.
2.  **`openwrt-library-arduino`**: La librería C++ para el sketch que se ejecuta en el MCU.
3.  **`luci-app-yunbridge`**: La interfaz de configuración web.
4.  **`openwrt-yun-examples-python`**: Paquete cliente con ejemplos de uso.
5.  **`openwrt-yun-core`**: Ficheros de configuración base del sistema.

> **Nota:** Todo el código Python empacado para OpenWRT se instala desde este repositorio. No es necesario, ni recomendable, ejecutar `pip install` en la Yún; las dependencias en tiempo de ejecución (`serial_asyncio`, stubs y utilidades) ya están incluidas bajo `openwrt-yun-bridge/yunbridge/vendor/` mientras que los paquetes nativos `python3-paho-mqtt` y `luaposix` se obtienen directamente de los feeds oficiales mediante `opkg`.

## Primeros Pasos

1.  **Compilar:** Ejecuta `./1_compile.sh` para preparar el SDK y compilar los paquetes IPK de OpenWRT.
2.  **Instalar:** Transfiere el proyecto a tu Yún y ejecuta `./3_install.sh` para instalar el software y las dependencias.
	- El script pedirá confirmación antes de lanzar `opkg upgrade`. Exporta `YUNBRIDGE_AUTO_UPGRADE=1` si necesitas ejecución no interactiva.
3.  **Configurar:** Accede a la interfaz web de LuCI en tu Yún, navega a `Services > YunBridge` y configura el daemon.
4.  **Explorar:** Revisa los ejemplos en `openwrt-yun-examples-python/` para aprender a interactuar con el puente a través de MQTT.

### Verificación y control de calidad

- **Tipado estático:** Ejecuta `pyright` en la raíz del repositorio antes de enviar parches; la configuración (`pyrightconfig.json`) está preparada para ignorar los ejemplos legacy y validar el daemon y sus utilidades.
- **Pruebas manuales:** Tras instalar los paquetes IPK en tu Yún, verifica el flujo end-to-end ejecutando uno de los scripts de `openwrt-yun-examples-python` y revisa el nuevo log del daemon (`/var/log/yunbridge.log`).
- **Monitoreo:** El daemon expone estados y errores del MCU en `br/system/status` (JSON) y publica el tamaño actual de la cola MQTT en `/tmp/yunbridge_status.json` junto al límite configurado.