
# YunBridge v2

Python3-based bridge daemon for Arduino Yun, with exclusive support for MQTT. Legacy examples and scripts (classic Bridge, REST, CGI) have been removed to advance the MQTT roadmap.

## Features
- MQTT client on /dev/ttyATH0 @ 250000 baud (adjust as needed)
- Modular, extensible, Python3 codebase
- Direct integration with MQTT broker and WebUI

## Dependencies
- Python 3 and pyserial must be installed on OpenWRT:
	```sh
	opkg update
	opkg install python3 python3-pyserial
	```

## Installation
See the unified `install.sh` in the repository root for complete installation of all components.

## Hardware Test
- The main example is generic pin control via MQTT (default: pin 13, but any pin can be used).
- Verify operation using the example scripts and WebUI MQTT.

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---

# Recommended Example

**For MQTT integration, use:**

`Bridge-v2/LED13BridgeControl.ino` (generic pin control via MQTT, default: 13)

All legacy examples and scripts have been removed. Only MQTT flows are supported.

# Hardware Tests

## Requirements
- Arduino Yun with OpenWRT and all v2 packages installed
- Arduino IDE, SSH, and web browser

## Main Test
1. **Generic Pin MQTT**
		- Upload `Bridge-v2/LED13BridgeControl.ino` to your Yun.
		- Run `YunBridge-v2/examples/led13_mqtt_test.py` on the Yun (SSH):
			```bash
			python3 /path/to/YunBridge-v2/examples/led13_mqtt_test.py [PIN]
			```
			(Replace `[PIN]` with the pin number you want to test, default is 13)
		- Open YunWebUI in your browser and use the ON/OFF buttons for the pin.
		- The selected pin should respond in all cases.

## Troubleshooting
- Ensure `/dev/ttyATH0` is present and free.
- Verify that the YunBridge daemon and the MQTT broker are running.

---

## Pending

// Official Mosquitto support with WebSockets on OpenWrt (cross-compilation and/or integration in scripts) will be added when supported by OpenWrt packages.

---
