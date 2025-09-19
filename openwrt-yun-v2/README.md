# openwrt-yun-v2

OpenWRT integration package para Bridge v2, YunBridge v2 y YunWebUI v2, con soporte exclusivo para MQTT. El soporte para ejemplos y scripts legacy ha sido eliminado para avanzar en el roadmap MQTT.

## Features
- Scripts y parches para OpenWRT moderno
- Configuración automática de /dev/ttyATH0 @ 250000 baud (ajustar según hardware)
- Scripts de instalación y arranque para YunBridge MQTT
- Integración Web UI/MQTT


## Dependencies
- Python 3 y pyserial deben estar instalados en OpenWRT:
	```sh
	opkg update
	opkg install python3 python3-pyserial
	```

## Installation
Ver `install.sh` para pasos de instalación y parches en OpenWRT.

## Prueba de hardware
- Incluye instrucciones para verificar el bridge MQTT y la Web UI

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)

---

# Pruebas de hardware

- Arduino Yun con OpenWRT y todos los paquetes v2 instalados

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
