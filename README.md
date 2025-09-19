# Arduino Yun v2 Ecosystem

This repository contains modernized, interoperable packages for Arduino Yun, now con soporte exclusivo para MQTT como protocolo de control y monitoreo en tiempo real. El soporte para ejemplos y scripts legacy (Bridge clásico, REST, CGI) ha sido eliminado para avanzar en el roadmap MQTT.

## Packages
- **Bridge-v2**: Arduino library (C++) for Yun, con soporte MQTT y ejemplos para integración IoT.
- **YunBridge-v2**: Daemon Python3 para OpenWRT, MQTT client, modular y extensible.
- **YunWebUI-v2**: Web UI moderna, MQTT client vía JavaScript para control y monitoreo en tiempo real.
- **openwrt-yun-v2**: Scripts de integración OpenWRT y automatización de instalación.


## Dependencies
- Python 3 y pyserial deben estar instalados en OpenWRT:
	```sh
	opkg update
	opkg install python3 python3-pyserial
	```

## Ejemplo recomendado

**Para integración MQTT, usa:**

`Bridge-v2/LED13BridgeControl.ino` (control de LED 13 vía MQTT)

Todos los ejemplos y scripts legacy han sido eliminados. Solo se soportan flujos MQTT.

## Installation Sequence
1. Flash your Yun with a modern OpenWRT image.
2. Instala **openwrt-yun-v2** (`/openwrt-yun-v2/install.sh`).
3. Instala **YunBridge-v2** (`/YunBridge-v2/install.sh`).
4. Instala **YunWebUI-v2** (`/YunWebUI-v2/install.sh`).
5. Instala la librería **Bridge-v2** en Arduino (`/Bridge-v2/install.sh`).
6. Sube el sketch MQTT de ejemplo y verifica operación vía MQTT/WebUI.

## Hardware Test
- El ejemplo principal es el control MQTT de LED 13.
- Verifica funcionamiento usando los scripts y WebUI MQTT.

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)


## MQTT Protocol Integration

Desde la versión 2.1+ el ecosistema solo soporta MQTT como protocolo para comunicación en tiempo real, lectura/escritura de pines e integración IoT.

### Arquitectura
- **MQTT Broker:** Local (OpenWRT/Mosquitto) o externo.
- **YunBridge-v2:** Cliente MQTT, suscribe/controla tópicos de pines, publica estados.
- **Bridge-v2:** Recibe comandos MQTT desde Linux, reporta cambios de estado.
- **YunWebUI-v2:** Cliente MQTT vía JavaScript para UI en tiempo real.

### Estructura de tópicos MQTT
- `yun/pin/<N>/set` — Payload: `ON`/`OFF` o `1`/`0` (set pin N)
- `yun/pin/<N>/state` — Payload: `ON`/`OFF` o `1`/`0` (estado actual)
- `yun/pin/<N>/get` — Solicita estado actual
- `yun/command` — Comandos avanzados

### Flujo de datos
1. WebUI publica `ON` en `yun/pin/13/set`.
2. Daemon recibe y envía comando MQTT al Arduino.
3. Arduino cambia el pin y confirma.
4. Daemon publica nuevo estado en `yun/pin/13/state`.
5. WebUI/MQTT client recibe y actualiza UI.

### Seguridad
- Soporte para autenticación MQTT (usuario/contraseña).
- Opcionalmente, TLS.

Ver `ROADMAP.md` para mejoras futuras.

---


# Pruebas de hardware

## Requisitos
- Arduino Yun con OpenWRT y todos los paquetes v2 instalados
- Arduino IDE, SSH y navegador web

## Prueba principal
1. **LED 13 MQTT**
		- Sube `Bridge-v2/LED13BridgeControl.ino` a tu Yun.
		- Ejecuta `YunBridge-v2/examples/led13_mqtt_test.py` en el Yun (SSH):
			```bash
			python3 /path/to/YunBridge-v2/examples/led13_mqtt_test.py
			```
		- Abre YunWebUI en tu navegador y usa los botones ON/OFF de LED 13.
		- El LED 13 debe responder en todos los casos.

## Troubleshooting
- Asegúrate de que `/dev/ttyATH0` está presente y libre.
- Verifica que el daemon YunBridge y el broker MQTT estén corriendo.

---
