
# openwrt-yun-v2

OpenWRT integration package for Bridge v2, YunBridge v2, and YunWebUI v2, with exclusive support for MQTT. Support for legacy examples and scripts has been removed to advance the MQTT roadmap.

## Features
- Scripts and patches for modern OpenWRT
- Automatic configuration of /dev/ttyATH0 @ 250000 baud (adjust according to hardware)
- Installation and startup scripts for YunBridge MQTT
- Web UI/MQTT integration

## Dependencies
Python 3 and pyserial must be installed on OpenWRT:
```sh
opkg update
opkg install python3 python3-pyserial
```

## Installation
See `install.sh` for installation steps and OpenWRT patches.

## Hardware Test
- Includes instructions to verify the MQTT bridge and Web UI

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)

---

# Hardware Tests

- Arduino Yun with OpenWRT and all v2 packages installed

## Main Test
1. **LED 13 MQTT**
    - Upload `Bridge-v2/LED13BridgeControl.ino` to your Yun.
    - Run `YunBridge-v2/examples/led13_mqtt_test.py` on the Yun (SSH):
      ```bash
      python3 /path/to/YunBridge-v2/examples/led13_mqtt_test.py
      ```
    - Open YunWebUI in your browser and use the ON/OFF buttons for LED 13.
    - LED 13 should respond in all cases.

## Troubleshooting
- Ensure `/dev/ttyATH0` is present and free.
- Verify that the YunBridge daemon and the MQTT broker are running.

---
