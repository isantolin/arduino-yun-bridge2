'''# Arduino Yún Bridge 2

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

**Yún Bridge 2 es un reemplazo moderno, robusto y de alto rendimiento para el sistema Bridge original de Arduino Yún.**

Este proyecto re-imagina la comunicación entre el microcontrolador (MCU) y el procesador Linux (MPU) en el Yún, reemplazando la antigua solución basada en `python-bridge` por un daemon asíncrono y un protocolo RPC binario eficiente.

## Características Principales

- **Protocolo RPC Binario:** Un protocolo a medida, rápido y robusto con enmarcado [COBS](https://en.wikipedia.org/wiki/Consistent_Overhead_Byte_Stuffing) y verificación de integridad CRC16. Ver la especificación completa en [`PROTOCOL.md`](PROTOCOL.md).
- **Daemon Asíncrono:** El `bridge_daemon.py` está construido sobre `asyncio` de Python, lo que le permite manejar eficientemente la I/O serie y de red sin bloqueos.
- **Integración con MQTT:** El daemon expone la funcionalidad del MCU (GPIO, ADC, etc.) a través de un broker MQTT, permitiendo que múltiples clientes se comuniquen con el hardware de forma desacoplada y escalable.
- **Interfaz Web LuCI:** Incluye una aplicación para LuCI, la interfaz de configuración estándar de OpenWRT, para una fácil configuración.
- **Ejemplos Modernos:** Los scripts de ejemplo están escritos con `asyncio` y `aiomqtt`, demostrando las mejores prácticas para interactuar con el puente.

## Arquitectura

El ecosistema se compone de varios paquetes cohesivos:

1.  **`openwrt-yun-bridge`**: El daemon principal de Python que se ejecuta en el MPU.
2.  **`openwrt-library-arduino`**: La librería C++ para el sketch que se ejecuta en el MCU.
3.  **`luci-app-yunbridge`**: La interfaz de configuración web.
4.  **`openwrt-yun-examples-python`**: Paquete cliente con ejemplos de uso.
5.  **`openwrt-yun-core`**: Ficheros de configuración base del sistema.

## Primeros Pasos

1.  **Compilar:** Ejecuta `./1. compile.sh` para compilar los paquetes IPK de OpenWRT.
2.  **Instalar:** Transfiere el proyecto a tu Yún y ejecuta `./3. install.sh` para instalar todo el software y las dependencias.
3.  **Configurar:** Accede a la interfaz web de LuCI en tu Yún, navega a `Services > YunBridge` y configura el daemon.
4.  **Explorar:** Revisa los ejemplos en `openwrt-yun-client-python/examples/` para aprender a interactuar con el puente a través de MQTT.

'''