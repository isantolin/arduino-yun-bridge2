

# openwrt-yun-v2

OpenWRT integration package for Bridge v2, YunBridge v2, and YunWebUI v2, with exclusive support for MQTT. Support for legacy examples and scripts has been removed to advance the MQTT roadmap.


## Features
  . The `pin` parameter is now required (no default).
- Serial communication is set to 250000 baud everywhere (daemon, CGI, Arduino sketches).

## Dependencies
Python 3 and pyserial must be installed on OpenWRT:
```sh
opkg update
opkg install python3 python3-pyserial
```

## Installation
See the unified `install.sh` in the repository root for complete installation of all components.

## Hardware Test
- Includes instructions to verify the MQTT bridge and Web UI

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)

---

# Hardware Tests

- Arduino Yun with OpenWRT and all v2 packages installed

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
