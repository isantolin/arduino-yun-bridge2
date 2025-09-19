# Bridge v2

Arduino library for Yun, con soporte exclusivo para MQTT. El soporte para ejemplos y sketches legacy (Bridge clásico) ha sido eliminado para avanzar en el roadmap MQTT.

## Features
- Soporte MQTT para integración IoT
- Ejemplo principal: control de LED 13 vía MQTT

## Installation
Ver `install.sh` para pasos de instalación de la librería Arduino.

## Prueba de hardware
- El ejemplo principal es el control MQTT de LED 13.

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---


# Pruebas de hardware

## Requisitos
- Arduino Yun con OpenWRT y todos los paquetes v2 instalados
- Arduino IDE para subir el sketch

## Prueba principal
1. **LED 13 MQTT**
	- Sube `Bridge-v2/LED13BridgeControl.ino` a tu Yun.
	- Ejecuta el test MQTT desde el Yun o la WebUI.
	- El LED 13 debe responder en todos los casos.

## Troubleshooting
- Asegúrate de que el daemon YunBridge y el broker MQTT estén corriendo.

---

---
