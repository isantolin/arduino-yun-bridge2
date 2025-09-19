# YunWebUI v2

Web interface para Arduino Yun, con soporte exclusivo para MQTT. El soporte para integración REST/CGI ha sido eliminado para avanzar en el roadmap MQTT.

## Features
- Web UI HTML5/JS/CSS
- Integración MQTT en tiempo real vía JavaScript

## Installation
Ver `install.sh` para pasos de despliegue en el web server de OpenWRT.

## Prueba de hardware
- Web UI incluye control MQTT de LED 13



## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)

---


# Pruebas de hardware

## Requisitos
- Arduino Yun con OpenWRT, YunWebUI-v2, YunBridge-v2 y Bridge-v2 instalados
- Navegador web en la misma red

## Prueba principal
1. **LED 13 MQTT Web Control**
	- Abre YunWebUI en tu navegador (por ejemplo, `http://yun.local/arduino-webui-v2/`).
	- Usa los botones ON/OFF de LED 13.
	- El LED 13 debe responder y el estado actualizarse en tiempo real.

## Troubleshooting
- Verifica que el daemon YunBridge y el broker MQTT estén corriendo.

---

---
