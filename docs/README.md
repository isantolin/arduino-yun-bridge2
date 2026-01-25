# Arduino MCU Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**MCU Bridge 2 es un reemplazo moderno, robusto y agnóstico de hardware para el sistema Bridge original.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en dispositivos OpenWrt, reemplazando la antigua solución basada en `python-bridge` por un daemon asíncrono y un protocolo RPC binario eficiente.

> **Nota de Migración (Enero 2026):**
> El proyecto ha sido renombrado de "Yun Bridge" a "MCU Bridge" para reflejar su capacidad de funcionar en cualquier dispositivo OpenWrt (no solo Arduino Yun) que disponga de un enlace serial con un MCU.
> - Paquetes OpenWrt: `openwrt-yun-*` ahora son `openwrt-mcu-*`
> - Módulos Python: `yunbridge` ahora es `mcubridge`
> - Configuración UCI: `yunbridge` ahora es `mcubridge`

## Características Principales

- **Límites configurables:** Los buffers interno de consola y mailbox se pueden ajustar vía UCI (`console_queue_limit_bytes`, `mailbox_queue_limit`, `mailbox_queue_bytes_limit`) para prevenir desbordes en escenarios con alto tráfico.
- **Backpressure en MQTT con MQTT v5:** El tamaño de la cola de publicación hacia el broker se controla con `mqtt_queue_limit`.
- **Respuestas correladas en MQTT:** Cada publicación originada por el daemon puede reutilizar el `response_topic` proporcionado por el cliente y propaga un `correlation_data` binario.
- **Handshake automático MCU ↔ Linux:** Tras cada reconexión, el daemon solicita `CMD_GET_VERSION` y publica la versión del firmware del sketch en `br/system/version/value`.
- **Protección ante frames serie malformados:** El lector COBS aplica un límite duro al tamaño de cada paquete.
- **Procesos asíncronos robustos:** Los polls sucesivos ahora entregan todo el `stdout`/`stderr` generado.
- **Estado inmediato de buzón:** Los sketches pueden invocar `Mailbox.requestAvailable()`.
- **Lecturas de pin dirigidas desde Linux:** `CMD_DIGITAL_READ`/`CMD_ANALOG_READ` solo se originan desde el daemon.

### Modernización Técnica (Enero 2026) - SIL-2 Ready

El stack ha sido reconstruido para cumplir con estándares de seguridad funcional y máximo rendimiento:

#### 1. Zero-Overhead Serial Driver (Python)
Se eliminó la dependencia de `pyserial`. El nuevo driver utiliza `asyncio` nativo con `termios` y `fcntl` directamente sobre el descriptor de archivo. Implementa **Eager Writes**: intenta escribir directamente al buffer del kernel (`os.write`), evitando el overhead del bucle de eventos para cargas bajas/medias.

#### 2. Gestión de Memoria Estática (C++)
La librería Arduino ha migrado completamente a **ETL (Embedded Template Library)**.
- **Cero `malloc`/`new`:** Toda la memoria se asigna estáticamente en tiempo de compilación.
- **Contenedores seguros:** Uso de `etl::vector` y `etl::circular_buffer` en lugar de arrays C crudos.
- **Determinismo:** El uso de memoria es constante y predecible, eliminando riesgos de fragmentación del heap.

#### 3. Persistencia en RAM (Python)
El sistema de colas persistentes (MQTT Spool) ha abandonado SQLite en favor de una cola de archivos en RAM (`/tmp`).
- **Formato:** `msgspec` (MsgPack optimizado) para serialización binaria ultrarrápida.
- **Cero escrituras en Flash:** El diseño garantiza que el spooling temporal nunca toque la memoria flash del router, preservando su vida útil.
- **Recuperación:** Si el broker MQTT cae, los mensajes se acumulan en RAM hasta que se restablece la conexión o se alcanza el límite de memoria.

#### 4. Concurrencia Estructurada y Resiliencia
- **Supervisión:** Uso estricto de `asyncio.TaskGroup` (Python 3.11+) para gestionar el ciclo de vida de tareas. No hay "fire-and-forget"; todas las tareas están supervisadas.
- **Scheduler Cooperativo (MCU):** Reemplazo del bucle `process()` monolítico por `TaskScheduler`. Tareas separadas para procesamiento serial y watchdog aseguran tiempos de respuesta deterministas.
- **Resiliencia Declarativa:** Decorador `@backoff` personalizado para reintentos exponenciales no bloqueantes, limpiando la lógica de conexión.

#### 5. Configuración Declarativa
Validación estricta de la configuración UCI mediante esquemas `marshmallow`. Errores de tipo o rango se detectan al inicio, y se imponen reglas de seguridad como prohibir rutas de spool fuera de `/tmp`.

## Arquitectura

1.  **`openwrt-mcu-bridge`**: El daemon principal de Python que se ejecuta en el MPU.
2.  **`openwrt-library-arduino`**: La librería C++ para el sketch que se ejecuta en el MCU.
3.  **`luci-app-mcubridge`**: La interfaz de configuración web.
4.  **`openwrt-mcu-examples-python`**: Paquete cliente con ejemplos de uso.
5.  **`openwrt-mcu-core`**: Ficheros de configuración base del sistema.

> ¿Buscas detalles adicionales sobre flujos internos, controles de seguridad, observabilidad y el contrato del protocolo? Revisa [`PROTOCOL.md`](PROTOCOL.md) para obtener el documento actualizado.

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

Si ya tienes OpenWrt instalado y no quieres reflashear:

1.  **Compilar paquetes:** Ejecuta `./1_compile.sh` para preparar el SDK y compilar los paquetes APK de OpenWRT (incluidas las dependencias Python en `feeds/`).
2.  **Preparar almacenamiento:** Ejecuta `./2_expand.sh` para configurar extroot en la tarjeta SD y crear swap (el sistema reiniciará).
3.  **Instalar:** Transfiere el proyecto a tu MCU y ejecuta `./3_install.sh` para instalar el software desde `bin/`.
	- El script evita hacer upgrades del sistema (por estabilidad) y se centra en instalar/actualizar las dependencias necesarias para McuBridge.

### Configuración post-instalación

1.  **Configurar:** Accede a la interfaz web de LuCI en tu MCU, navega a `Services > McuBridge` y configura el daemon. Antes de ponerlo en producción usa la pestaña *Credentials & TLS* (o `../tools/rotate_credentials.sh --host <mcu>`) para rotar el secreto serie y las credenciales MQTT directamente en UCI.
2.  **Explorar:** Revisa los ejemplos en `openwrt-mcu-examples-python/` para aprender a interactuar con el puente a través de MQTT.

### Verificación y control de calidad

- **Tipado estático:** Ejecuta `pyright` en la raíz del repositorio antes de enviar parches.
- **Guardia del protocolo:** Corre `tox -e protocol` para validar consistencia.
- **Cobertura Python:** Lanza `tox -e coverage` para generar reportes en `coverage/python/`.
- **Cobertura C++:** Ejecuta `./tools/coverage_arduino.sh` para compilar un harness host.
- **Matriz Python 3.13:** Usa `tox` para ejecutar la suite completa y detectar regresiones antes de desplegar:
	```sh
	tox -e py313
	```
- **Smoke test remoto:** `./tools/hardware_smoke_test.sh --host <mcu>` invoca `/usr/bin/mcubridge-hw-smoke` vía SSH.
- **Pruebas manuales:** Tras instalar los paquetes en tu MCU, verifica el flujo end-to-end ejecutando uno de los scripts de `openwrt-mcu-examples-python` y revisa los logs del daemon con `logread | grep mcubridge`.
- **Diagnóstico en el MCU:** Carga el sketch `openwrt-library-arduino/examples/FrameDebug/FrameDebug.ino`.
- **Monitoreo:** El daemon expone estados y errores del MCU en `br/system/status` (JSON).
