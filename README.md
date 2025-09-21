# Unified Installation

To install the entire Arduino Yun v2 ecosystem (daemon, scripts, configs, Arduino library):

```sh
git clone https://github.com/isantolin/arduino-yun-bridge2.git
cd arduino-yun-bridge2
sh install.sh
```

This script will:
- Update and upgrade OpenWRT
- Install all dependencies (python3, pyserial, mosquitto, luci)
- Install daemon, scripts, configs, and Arduino library
- Start the YunBridge daemon

After running the script, upload the example sketch from Bridge-v2 to your Yun using the Arduino IDE, reboot if needed, and test MQTT/WebUI integration.

# Arduino Yun v2 Ecosystem

This repository contains all the components for a modern MQTT-based solution for Arduino Yun v2, including the bridge daemon, OpenWrt scripts, LuCI web panel, and examples.

## Packages
- **Bridge-v2**: Arduino library (C++) for Yun, with MQTT support and IoT integration examples.
- **YunBridge-v2**: Python3 daemon for OpenWRT, MQTT client, modular and extensible.
- **openwrt-yun-v2**: OpenWRT integration scripts and automated installation.
- **Web UI**: Now integrated into the LuCI panel (iframe). All configuration is managed via the LuCI panel and stored in UCI (`/etc/config/yunbridge`). The YunBridge daemon reads UCI configuration using python3-uci (with fallback to defaults). The YunWebUI-v2 package has been removed.

## Dependencies
Python 3 and pyserial must be installed on OpenWRT:
```sh
opkg update
opkg install python3 python3-pyserial
```

## Recommended Example
For MQTT integration, use:

`Bridge-v2/LED13BridgeControl.ino` (control LED 13 via MQTT)

All legacy examples and scripts have been removed. Only MQTT flows are supported.

## Installation Sequence
1. Flash your Yun with a modern OpenWRT image.
2. Install **openwrt-yun-v2** (`/openwrt-yun-v2/install.sh`).
3. Install **YunBridge-v2** (`/YunBridge-v2/install.sh`).
4. Install the **Bridge-v2** library in Arduino (`/Bridge-v2/install.sh`).
5. Upload the example MQTT sketch and verify operation via MQTT/WebUI.

## Hardware Test
- The main example is MQTT control of LED 13.
- Verify operation using the scripts and WebUI MQTT.

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)

## MQTT Protocol Integration

From version 2.1+, the ecosystem only supports MQTT as the protocol for real-time communication, pin read/write, and IoT integration.

### Architecture
- **MQTT Broker:** Local (OpenWRT/Mosquitto) or external.
- **YunBridge-v2:** MQTT client, subscribes/controls pin topics, publishes states.
- **Bridge-v2:** Receives MQTT commands from Linux, reports state changes.
- **Web UI:** MQTT client via JavaScript for real-time UI.

### MQTT Topic Structure
- `yun/pin/<N>/set` — Payload: `ON`/`OFF` or `1`/`0` (set pin N)
- `yun/pin/<N>/state` — Payload: `ON`/`OFF` or `1`/`0` (current state)
- `yun/pin/<N>/get` — Request current state
- `yun/command` — Advanced commands

### Data Flow
1. WebUI publishes `ON` to `yun/pin/13/set`.
2. Daemon receives and sends MQTT command to Arduino.
3. Arduino changes the pin and confirms.
4. Daemon publishes new state to `yun/pin/13/state`.
5. WebUI/MQTT client receives and updates the UI.

### Security
- Support for MQTT authentication (username/password).
- Optionally, TLS.

See `ROADMAP.md` for future improvements.

---

# Hardware Tests

## Requirements
- Arduino Yun with OpenWRT and all v2 packages installed
- Arduino IDE, SSH, and web browser

## Main Test
1. **LED 13 MQTT**
    - Upload `Bridge-v2/LED13BridgeControl.ino` to your Yun.
    - Run `YunBridge-v2/examples/led13_mqtt_test.py` on the Yun (SSH):
      ```bash
      python3 /path/to/YunBridge-v2/examples/led13_mqtt_test.py
      ```
    - Open the WebUI in your browser and use the ON/OFF buttons for LED 13.
    - LED 13 should respond in all cases.

## Troubleshooting
- Ensure `/dev/ttyATH0` is present and free.
- Verify that the YunBridge daemon and the MQTT broker are running.

---
---
