

# Arduino Yun v2 Ecosystem (Unified Documentation)

## Quick Start

1. Clone the repository and run the installer:
  ```sh
  git clone https://github.com/isantolin/arduino-yun-bridge2.git
  cd arduino-yun-bridge2
  sh install.sh
  ```
2. Upload the main sketch `LED13BridgeControl.ino` (at the project root) to your Yun using the Arduino IDE.
3. Open the Web UI (LuCI) at `http://<yun-ip>/cgi-bin/luci/admin/services/yunbridge`.
4. Test MQTT and Web UI control of your pins.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Installation & Dependencies](#1-installation--dependencies)
3. [Architecture & Components](#2-architecture--components)
4. [MQTT Usage & Examples](#3-mqtt-usage--examples)
5. [Hardware Tests](#4-hardware-tests)
6. [Troubleshooting](#5-troubleshooting)
7. [Roadmap & Links](#6-roadmap--links)

---

# Arduino Yun v2 Ecosystem (Unified Documentation)

## 1. Installation & Dependencies

## 1. Installation & Dependencies
To install the entire Arduino Yun v2 ecosystem (daemon, scripts, configs, Arduino library):

To install the entire Arduino Yun v2 ecosystem (daemon, scripts, configs, Arduino library):
```sh

```sh
git clone https://github.com/isantolin/arduino-yun-bridge2.git
cd arduino-yun-bridge2
sh install.sh
```
This script will:

This script will:
- Update and upgrade OpenWRT
- Install all dependencies (python3, pyserial, mosquitto, luci)
- Install daemon, scripts, configs, and Arduino library
- Start the YunBridge daemon
**Dependencies:**

**Dependencies:**
- Python 3, pyserial, paho-mqtt, python3-uci must be installed on OpenWRT:
  ```sh
  opkg update
  opkg install python3 python3-pyserial mosquitto luci
  pip3 install paho-mqtt python3-uci
  ```
  Or use the provided `setup.py` in `openwrt-yun-bridge` for Python dependencies:
  ```sh
  cd openwrt-yun-bridge
  python3 setup.py install
  ```
## Performance & Logging

**Async Logging:**
The YunBridge daemon now uses asynchronous logging with a configurable buffer size for high performance. Log writes are buffered and flushed in a background thread, minimizing I/O overhead.

- Default log buffer size: 50 lines
- Change buffer size by setting the environment variable `YUNBRIDGE_LOG_BUFFER_SIZE` before starting the daemon:
  ```sh
  export YUNBRIDGE_LOG_BUFFER_SIZE=100
  /etc/init.d/yunbridge start
  ```
- Log file: `/tmp/yunbridge_debug.log`

**Performance Improvements:**
- All serial and MQTT operations are non-blocking and efficient.
- The main loop is robust, with minimal CPU usage and no unnecessary polling.
- Thread usage is minimal: only the main thread, MQTT thread, and log thread are used.
- All code is PEP8-compliant and modular.
### OpenWRT Integration Details

### OpenWRT Integration Details
The `openwrt-yun-core/package` directory contains scripts and config files to ensure Bridge v2 and YunBridge v2 work on modern OpenWRT:

The `openwrt-yun-core/package` directory contains scripts and config files to ensure Bridge v2 and YunBridge v2 work on modern OpenWRT:
- `yunbridge.init`: Init script to start/stop YunBridge daemon

- `yunbridge.init`: Init script to start/stop YunBridge daemon
- `99-yunbridge-ttyath0.conf`: UCI config for serial port
- `yunbridge.files`: List of files for package manager

**Manual Installation Steps (if needed):**

**Manual Installation Steps (if needed):**
1. Copy all files to your OpenWRT device in the appropriate locations:
  - `/usr/bin/yunbridge` (daemon)
  - `/etc/init.d/yunbridge` (init script)
  - `/etc/config/yunbridge-ttyath0` (serial config)
  - `/www/cgi-bin/pin` (CGI script, replaces the old led13)
2. Make scripts executable:
  ```sh
  chmod +x /etc/init.d/yunbridge /www/cgi-bin/pin
  ```
3. Enable and start the service:
  ```sh
  /etc/init.d/yunbridge enable
  /etc/init.d/yunbridge start
  ```

**Gestión de configuración UCI (ejemplos):**

**UCI Configuration Management (examples):**
Para ver la configuración actual del puerto serie:

To view the current serial port configuration:
```sh
uci show yunbridge-ttyath0
```

To view the main daemon configuration:
```sh
uci show yunbridge
```

To change the baudrate (example to 57600):
```sh
uci set yunbridge-ttyath0.bridge_serial.baudrate='57600'
uci commit yunbridge-ttyath0
```

To list all relevant UCI config files:
```sh
ls /etc/config/yunbridge*
```

Remember to restart the daemon after changing the configuration:
```sh
/etc/init.d/yunbridge restart
```
-- Ensure `/dev/ttyATH0` exists and is not used by other processes.

- Ensure `/dev/ttyATH0` exists and is not used by other processes.
- Check `/etc/inittab` and `/etc/config/system` for serial port conflicts.
- Use UCI config to adjust baudrate if needed.
After running the script, upload the main sketch `LED13BridgeControl.ino` (at the project root) to your Yun using the Arduino IDE, reboot if needed, and test MQTT/WebUI integration.

After running the script, upload the main sketch `LED13BridgeControl.ino` (at the project root) to your Yun using the Arduino IDE, reboot if needed, and test MQTT/WebUI integration.



## 2. Architecture & Components


The repository is now organized into the following main components:


- **Core Yun/OpenWRT (openwrt-yun-core):**
  - Scripts, configuration, and integration for the Arduino Yun/OpenWRT core.
  - Includes daemon, startup scripts, UCI configuration, system integration.
  - Does not include LuCI or Web UI.

- **Arduino Library (openwrt-library-arduino):**
  - YunBridge library for Arduino Yun.
  - Install the library in your IDE using `openwrt-library-arduino/install.sh`.
  - Example sketches in `openwrt-yun-client-sketches/examples/`.

- **Python MQTT Examples (openwrt-yun-client-python):**
  - Example scripts to control the Yun via MQTT.
  - All examples are in `openwrt-yun-client-python/`.

- **Arduino Sketch Examples (openwrt-yun-client-sketches/examples):**
  - Example sketches for testing and validation.
  - All example sketches are in `openwrt-yun-client-sketches/examples/`.

- **LuCI App (luci-app-yunbridge):**
  - Standalone package for LuCI (Web UI) and advanced configuration.
  - All LuCI code and files are in `/luci-app-yunbridge`.
  - Installation and maintenance are separate.


### Core Installation (openwrt-yun-core)
Follow the instructions in `install.sh` to install the core, daemon, and scripts.



### Web UI Installation (luci-app-yunbridge)


**Recommended option: install from .ipk package**


1. Build the `.ipk` package for `luci-app-yunbridge` in an OpenWRT buildroot:
  - Copy the `luci-app-yunbridge` folder to `package/` inside your OpenWRT tree.
  - Run:
    ```sh
    make package/luci-app-yunbridge/compile V=s
    ```
  - The `.ipk` file will appear in `bin/packages/<arch>/luci/`.
2. Copy the `.ipk` to your Yun/OpenWRT and run:
  ```sh
  opkg install luci-app-yunbridge_*.ipk
  ```


**Manual option (only if you don't have the .ipk):**


Follow the instructions in `/luci-app-yunbridge/README.md` to install the web interface and advanced configuration by manually copying the files.




**Notes:**
- The YunBridge daemon reads configuration from UCI (`/etc/config/yunbridge`) using `python3-uci`. If an option does not exist, the default value is used.
- The LuCI package (Web UI) is optional and can be installed/uninstalled independently. Use the `.ipk` for easiest install, or copy files manually if needed.
- If the installer detects a `.ipk` for `luci-app-yunbridge`, it will install it automatically with `opkg install`. If not, it will attempt manual file installation.


All legacy examples and scripts have been removed. Only MQTT flows are supported.


## 3. MQTT Usage & Examples

### Basic MQTT Pin Control Example

To turn ON pin 13:
```sh
mosquitto_pub -h <yun-ip> -t yun/pin/13/set -m ON
```
To turn OFF pin 13:
```sh
mosquitto_pub -h <yun-ip> -t yun/pin/13/set -m OFF
```
To get the state of pin 13:
```sh
mosquitto_pub -h <yun-ip> -t yun/pin/13/get -m ""
mosquitto_sub -h <yun-ip> -t yun/pin/13/state
```

### Web UI Usage

1. Open the LuCI Web UI at `http://<yun-ip>/cgi-bin/luci/admin/services/yunbridge`.
2. Use the configuration panel to set MQTT and serial parameters.
3. Use the "Daemon Status" panel to check daemon health and logs.
4. Use the Web UI controls to toggle pins and monitor state in real time.

### MQTT Topic Schemas

#### Pin Control
  - Topic: `yun/pin/<N>/set`  (e.g. `yun/pin/13/set`)
  - Payload: `ON`/`OFF` or `1`/`0`
  - Topic: `yun/pin/<N>/get`
  - Payload: (any, triggers state publish)
  - Topic: `yun/pin/<N>/state`
  - Payload: `ON`/`OFF` or `1`/`0`

#### Advanced Commands
  - Topic: `yun/command`
  - Payloads:
    - `SET <key> <value>`: Store a key-value pair
    - `GET <key>`: Retrieve a value
    - `WRITEFILE <path> <data>`: Write data to file
    - `READFILE <path>`: Read file contents
  - `MAILBOX <msg>`: (legacy, ahora migrado a MQTT)
    - `RUN <cmd>`: Run a shell command
    - `CONSOLE <msg>`: Print to console

#### Daemon Topic Subscriptions

#### Example Flows
  - Publish `ON` to `yun/pin/13/set`
  - Publish any payload to `yun/pin/7/get`
  - Listen for state on `yun/pin/7/state`
  - Publish `SET foo bar` to `yun/command`
  - Publish `RUN echo hello` to `yun/command`



#### Example of arbitrary messages (new MQTT flow)
- To send a message to the Arduino from any MQTT client:
- Publish the text to the topic: `yun/mailbox/send`
- The Arduino will receive the message as `MAILBOX <msg>` via Serial1 and display it on the console.
- For the Arduino to send a message to other MQTT clients:
- The sketch must send via Serial1: `MAILBOX <msg>`
- The daemon will publish that message to the topic: `yun/mailbox/recv`
- Updated example: `openwrt-yun-client-python/mailbox_mqtt_test.py`

All scripts use the same topics and MQTT logic as the daemon and Arduino code. See each script for usage examples.

#### Architecture Overview

#### Architecture Overview
- **MQTT Broker:** Local (OpenWRT/Mosquitto) or external.
- **YunBridge Daemon:** MQTT client, subscribes/controls pin topics, publishes states.
- **Arduino Library:** Receives MQTT commands from Linux, reports state changes.
- **Web UI:** MQTT client via JavaScript for real-time UI.

#### Data Flow

#### Security

## 4. Hardware Tests

### Requirements

### Main Test
1. **Generic Pin MQTT**
    - Upload `LED13BridgeControl.ino` (at the project root) to your Yun using the Arduino IDE.
    - Run `openwrt-yun-client-python/led13_mqtt_test.py` on the Yun (SSH):
      ```bash
      python3 openwrt-yun-client-python/led13_mqtt_test.py [PIN]
      ```
      (Replace `[PIN]` with the pin number you want to test, default is 13)
    - Open the WebUI in your browser and use the ON/OFF buttons for the pin.
    - The selected pin should respond in all cases.


## 5. Troubleshooting

### Common Issues

- **Serial port not found:**
  - Ensure `/dev/ttyATH0` exists and is not used by another process.
  - Check `/etc/inittab` and `/etc/config/system` for serial port conflicts.
  - Use UCI config to adjust baudrate if needed.
- **MQTT not working:**
  - Verify the broker is running (`ps | grep mosquitto`).
  - Check MQTT host/port in the LuCI config.
  - Use `mosquitto_sub` and `mosquitto_pub` to test topics manually.
- **Web UI not loading:**
  - Make sure the LuCI package is installed and enabled.
  - Clear browser cache or try a different browser.
- **Daemon not starting:**
  - Check `/tmp/yunbridge_debug.log` for errors.
  - Run `/etc/init.d/yunbridge restart` and check status panel in LuCI.
- **Configuration changes not applied:**
  - Always restart the daemon after changing UCI config: `/etc/init.d/yunbridge restart`

For more help, see the log and status panels in the Web UI, or open an issue on GitHub.


## 6. Roadmap & Links


## 7. Roadmap & Links

See `ROADMAP.md` for future improvements and planned features. All completed items have been removed from the roadmap.

### Documentation

