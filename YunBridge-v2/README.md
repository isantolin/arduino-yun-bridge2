# YunBridge v2

Python3-based bridge daemon for Arduino Yun, con soporte exclusivo para MQTT. El soporte para ejemplos y scripts legacy (Bridge clásico, REST, CGI) ha sido eliminado para avanzar en el roadmap MQTT.

## Features
- Cliente MQTT en /dev/ttyATH0 @ 250000 baud (ajustar según tu hardware)
- Modular, extensible, Python3 codebase
- Integración directa con broker MQTT y WebUI


## Dependencies
- Python 3 y pyserial deben estar instalados en OpenWRT:
	```sh
	opkg update
	opkg install python3 python3-pyserial
	```

## Installation
Ver `install.sh` para instrucciones paso a paso.

## Prueba de hardware
- El ejemplo principal es el control MQTT de LED 13.
- Verifica funcionamiento usando los scripts y WebUI MQTT.

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---




# Ejemplo recomendado

**Para integración MQTT, usa:**

`Bridge-v2/LED13BridgeControl.ino` (control de LED 13 vía MQTT)

Todos los ejemplos y scripts legacy han sido eliminados. Solo se soportan flujos MQTT.


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

## Pendientes

- Soporte oficial de Mosquitto con Websockets en OpenWrt (compilación cruzada y/o integración en scripts)

---

---
