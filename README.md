
# Arduino Yun v2 Ecosystem (Unified Documentation)

## 1. Installation & Dependencies

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

**Dependencies:**
- Python 3 and pyserial must be installed on OpenWRT:
  ```sh
  opkg update
  opkg install python3 python3-pyserial
  ```

After running the script, upload the example sketch from Bridge-v2 to your Yun using the Arduino IDE, reboot if needed, and test MQTT/WebUI integration.

## 2. Architecture & Components

This repository contains all the components for a modern MQTT-based solution for Arduino Yun v2, including:

- **Bridge-v2**: Arduino library (C++) for Yun, with MQTT support and IoT integration examples. Main example: generic pin control via MQTT (default: pin 13, but any pin can be used).
- **YunBridge-v2**: Python3 daemon for OpenWRT, MQTT client, modular and extensible. Integrates with the MQTT broker and WebUI.
- **openwrt-yun-v2**: OpenWRT integration scripts and automated installation. Ensures all dependencies and configs are set up.
- **Web UI**: Integrated into the LuCI panel (iframe). All configuration is managed via the LuCI panel and stored in UCI (`/etc/config/yunbridge`). The YunBridge daemon reads UCI configuration using python3-uci (with fallback to defaults).

All legacy examples and scripts have been removed. Only MQTT flows are supported.

## 3. MQTT Usage & Examples

### MQTT Topic Schemas

#### Pin Control
- **Set pin state:**
  - Topic: `yun/pin/<N>/set`  (e.g. `yun/pin/13/set`)
  - Payload: `ON`/`OFF` or `1`/`0`
- **Get pin state:**
  - Topic: `yun/pin/<N>/get`
  - Payload: (any, triggers state publish)
- **Pin state update:**
  - Topic: `yun/pin/<N>/state`
  - Payload: `ON`/`OFF` or `1`/`0`

#### Advanced Commands
- **General command topic:**
  - Topic: `yun/command`
  - Payloads:
    - `SET <key> <value>`: Store a key-value pair
    - `GET <key>`: Retrieve a value
    - `WRITEFILE <path> <data>`: Write data to file
    - `READFILE <path>`: Read file contents
    - `MAILBOX SEND <msg>`: Send to mailbox
    - `MAILBOX RECV`: Receive from mailbox
    - `RUN <cmd>`: Run a shell command
    - `CONSOLE <msg>`: Print to console

#### Daemon Topic Subscriptions
- Subscribes: `yun/pin/+/set`, `yun/pin/+/get`, `yun/command`
- Publishes: `yun/pin/<N>/state`, responses to `yun/command` (future: `yun/command/response`)

#### Example Flows
- **Turn pin 13 ON:**
  - Publish `ON` to `yun/pin/13/set`
- **Get pin 7 state:**
  - Publish any payload to `yun/pin/7/get`
  - Listen for state on `yun/pin/7/state`
- **Set key-value:**
  - Publish `SET foo bar` to `yun/command`
- **Run process:**
  - Publish `RUN echo hello` to `yun/command`

#### Example Scripts
- `YunBridge-v2/examples/led13_mqtt_test.py`: Control and monitor any pin (default: 13)
- `YunBridge-v2/examples/all_mqtt_features_test.py`: Test all MQTT features (pin, kv, file, mailbox, process)
- `YunBridge-v2/examples/console_mqtt_test.py`: Send console commands
- `YunBridge-v2/examples/fileio_mqtt_test.py`: Test file I/O
- `YunBridge-v2/examples/kv_store_mqtt_test.py`: Test key-value store
- `YunBridge-v2/examples/mailbox_mqtt_test.py`: Test mailbox
- `YunBridge-v2/examples/process_mqtt_test.py`: Test process execution

All scripts use the same topic schemas as the daemon and Arduino code. See each script for usage examples.

#### Architecture Overview
- **MQTT Broker:** Local (OpenWRT/Mosquitto) or external.
- **YunBridge-v2:** MQTT client, subscribes/controls pin topics, publishes states.
- **Bridge-v2:** Receives MQTT commands from Linux, reports state changes.
- **Web UI:** MQTT client via JavaScript for real-time UI.

#### Data Flow
1. WebUI publishes `ON` to `yun/pin/13/set`.
2. Daemon receives and sends MQTT command to Arduino.
3. Arduino changes the pin and confirms.
4. Daemon publishes new state to `yun/pin/13/state`.
5. WebUI/MQTT client receives and updates the UI.

#### Security
- Support for MQTT authentication (username/password).
- Optionally, TLS.

## 4. Hardware Tests

### Requirements
- Arduino Yun with OpenWRT and all v2 packages installed
- Arduino IDE, SSH, and web browser

### Main Test
1. **Generic Pin MQTT**
    - Upload `Bridge-v2/LED13BridgeControl.ino` to your Yun.
    - Run `YunBridge-v2/examples/led13_mqtt_test.py` on the Yun (SSH):
      ```bash
      python3 /path/to/YunBridge-v2/examples/led13_mqtt_test.py [PIN]
      ```
      (Replace `[PIN]` with the pin number you want to test, default is 13)
    - Open the WebUI in your browser and use the ON/OFF buttons for the pin.
    - The selected pin should respond in all cases.

## 5. Troubleshooting

- Ensure `/dev/ttyATH0` is present and free.
- Verify that the YunBridge daemon and the MQTT broker are running.

## 6. Roadmap & Links

See `ROADMAP.md` for future improvements and planned features.

### Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---
