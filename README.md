# Arduino Yun v2 Ecosystem (Unified Documentation)

## New Modular Build & Install Workflow (2024)

This project now uses a modular, package-centric build and install system. All build and installation logic is handled by package-local Makefiles and setup.py files. Device-side installation is minimal and only installs pre-built artifacts. All Python packages are installed system-wide; virtualenv or venv is not used.

### Key Changes

- **All build logic is in each package's Makefile or setup.py** (no duplication, no global install logic)
- **`compile.sh`** automates local build of all packages and outputs .ipk/.whl artifacts to `bin/`
- **`install.sh`** is now minimal: it only installs pre-built .ipk/.whl files and performs no system setup
- **System-level setup (pip upgrade, etc.) is handled by the OpenWRT package postinst**
- **All dependencies are declared in the relevant Makefile or setup.py** (system and Python deps)
- **All logs and /tmp are stored on the SD card** (via bind mount on `/mnt/sda1`)

---

## Quick Start (2024+)

1. **Clone and build all packages locally:**
   ```sh
   git clone https://github.com/isantolin/arduino-yun-bridge2.git
   cd arduino-yun-bridge2
   ./compile.sh
   # All .ipk and .whl files will be in bin/
   ```
2. **Copy the `bin/` directory to your Yun/OpenWRT device.**
3. **Run the installer on the device:**
   ```sh
   sh install.sh
   ```
  - This will install only the pre-built .ipk/.whl files. No virtualenv or venv is used; all Python packages are installed system-wide.
4. **Upload the main sketch `LED13BridgeControl.ino` to your Yun using the Arduino IDE.**
5. **Open the Web UI (LuCI) at `http://<yun-ip>/cgi-bin/luci/admin/services/yunbridge`.**
6. **Test MQTT and Web UI control of your pins.**

---

## Build System Details

  - **`compile.sh`**: Installs required build dependencies on the development PC (Ubuntu/Debian/Fedora) and then builds all packages, leaving the artifacts in `bin/`.
  - **`install.sh`**: Should only be run on the Yun/OpenWRT device. Installs precompiled packages (.ipk, .whl) and activates the Python environment, without installing build dependencies or development packages.
  - **Each package** (e.g., `openwrt-yun-core`, `openwrt-yun-bridge`, `openwrt-yun-client-python`, `luci-app-yunbridge`) has its own Makefile or setup.py with all dependencies and installation logic.
  - **No global installation logic**: Everything is handled locally in each package.
  - **System configuration (pip upgrade, etc.) is performed in the postinst script of the OpenWRT package (`openwrt-yun-core/package/postinst`).**

---

## Directory Structure (2024+)

```
arduino-yun-bridge2/
  compile.sh           # Build all packages locally

  # Arduino Yun v2 Bridge (Bridge2)

  ## Overview

  Bridge2 is a robust, modern, and fully open-source bridge for the Arduino Yun v2 ecosystem. It enables seamless communication between the OpenWRT (Linux) side and the ATmega32U4 (Arduino) side, using MQTT as the only supported backend. All configuration is handled via UCI, and all logs are written to `/tmp` or SD card. The system is designed for reliability, maintainability, and ease of use, with a focus on modern Python 3 and OpenWRT best practices.

  **Key Features:**
  - MQTT-only backend (no Google Pub/Sub, no AWS SNS, no gRPC)
  - All configuration via UCI (no config files, no environment variables required)
  - Robust logging to `/tmp` or SD card, with immediate log file creation in debug mode
  - No Python virtualenvs: all Python code runs system-wide with Python 3.11+
  - Fully modular build/install scripts for OpenWRT and Python packages
  - Persistent swap file for reliable package installation on low-memory devices
  - LuCI web UI for configuration, status, and log viewing
  - All code and documentation in English

  ## Repository Structure

  ```
  Bridge-v2/                # Arduino sketch and C++ bridge library
    install.sh              # Arduino-side install script
    LED13BridgeControl.ino  # Example sketch
    src/                    # C++ source code
    examples/               # Arduino examples

  openwrt-yun-v2/           # OpenWRT-side system integration
    install.sh              # System install script
    package/                # OpenWRT package files (init, config, etc)
    scripts/                # System scripts (LED control, REST CGI, etc)

  YunBridge-v2/             # Python client and daemon
    install.sh              # Python-side install script
    src/                    # Python source code (bridge_daemon.py)
    examples/               # Python example scripts

  YunWebUI-v2/              # Web UI (LuCI)
    install.sh              # Web UI install script
    www/                    # Web assets (index.html, etc)
  ```

  ## Quick Start

  ### 1. Build Packages

  Use the provided `compile.sh` script to build all required packages and artifacts. This will generate:
  - OpenWRT .ipk packages (for system and daemon)
  - Python .whl package (for client)

  ```
  ./compile.sh
  ```

  ### 2. Install on OpenWRT

  Use the provided `install.sh` script to install all .ipk and .whl artifacts, configure the system, and set up swap if needed. This script is idempotent and safe to run multiple times.

  ```
  cd openwrt-yun-v2
  sudo ./install.sh
  ```

  ### 3. Configure via UCI

  All runtime configuration is handled via UCI. Example:

  ```
  uci set yunbridge.@bridge[0].serial_port='/dev/ttyATH0'
  uci set yunbridge.@bridge[0].mqtt_host='192.168.1.100'
  uci set yunbridge.@bridge[0].mqtt_user='myuser'
  uci set yunbridge.@bridge[0].mqtt_pass='mypassword'
  uci set yunbridge.@bridge[0].debug='1'  # Enable debug mode
  uci commit yunbridge
  /etc/init.d/yunbridge restart
  ```

  ### 4. Access the Web UI

  The LuCI web UI is available at `http://<device-ip>/cgi-bin/luci/admin/services/yunbridge`.

  ## Features & Architecture

  - **MQTT-Only Backend:**
    - All communication between the Linux and Arduino side is via MQTT. No support for Google Pub/Sub, AWS SNS, or gRPC.
  - **UCI-Based Configuration:**
    - All options (serial port, MQTT host, username, password, debug) are set via UCI. No config files or environment variables required.
  - **Robust Logging:**
    - All logs are written to `/tmp` or SD card. In debug mode, logs are flushed immediately for real-time debugging.
    - Log files:
      - `/tmp/yunbridge_daemon.log` (main daemon)
      - `/tmp/yunbridge_mqtt_plugin.log` (MQTT plugin)
      - `/tmp/yunbridge_script.log` (REST/CGI scripts)
      - `/tmp/yunbridge_debug.log` (debug log, immediate in debug mode)
      - `/tmp/yunbridge_status.json` (status file)
  - **No Virtualenvs:**
    - All Python code runs system-wide with Python 3.11+ and pip. No virtualenvs are used or required.
  - **Persistent Swap:**
    - A swap file is created at `/overlay/swapfile` and activated before any resource-intensive installs. Swap is made persistent via `/etc/fstab` and `/etc/rc.local`.
  - **LuCI Web UI:**
    - Provides configuration, status, and log viewing. All dependencies are installed automatically.
  - **Modular Build/Install:**
    - All build and install logic is handled by `compile.sh` and `install.sh`. No manual steps required.
  - **Clean Codebase:**
    - All legacy code and documentation for Pub/Sub, SNS, and gRPC have been removed. All code and comments are in English.

  ## Troubleshooting

  - **Logs not appearing?**
    - Ensure debug mode is enabled via UCI (`uci set yunbridge.@bridge[0].debug='1'`).
    - Check that `/tmp` is writable and has free space.
    - Restart the daemon: `/etc/init.d/yunbridge restart`
  - **MQTT connection issues?**
    - Check your MQTT broker address, username, and password in UCI.
    - Ensure the broker allows connections from the device IP.
    - Use `mosquitto_sub` and `mosquitto_pub` to test connectivity.
  - **Daemon not starting?**
    - Check `/tmp/yunbridge_daemon.log` for errors.
    - Ensure all dependencies are installed (`python3`, `pyserial`, `paho-mqtt`).
    - Run `opkg update && opkg install python3 python3-pip python3-pyserial python3-paho-mqtt` if needed.
  - **Web UI not loading?**
    - Ensure LuCI and all dependencies are installed (`luci-base`, `luci-compat`, `luci-mod-admin-full`, `lua`, `luci-lib-nixio`, `luci-lib-json`).
    - Re-run `install.sh` to fix missing dependencies.

  ## Development Notes

  - All build logic is in package-local Makefiles and `setup.py`.
  - All .ipk and .whl files are built locally and installed via `install.sh`.
  - No wheels are built on the device.
  - All configuration is via UCI; no config files or environment variables are used at runtime.
  - All logs are written to `/tmp` or SD card.
  - Only MQTT is supported as a backend.
  - All code and documentation is in English.

  ## License

  This project is licensed under the MIT License.

**Configuration validation:**
- The daemon and plugins will not start if required config values are missing or invalid. Errors are logged and shown in the console.



### Amazon SNS Support (Optional)

The daemon supports Amazon SNS in addition to MQTT. You can enable SNS messaging for cloud integration and hybrid workflows.

**Requirements:**
- Python package: `boto3` (installed by the installer)
- AWS account with SNS topic
- AWS credentials (Access Key ID and Secret Access Key)
- SNS Topic ARN and AWS region

**Configuration via LuCI Web UI:**
You can configure all SNS options directly from the YunBridge LuCI interface:

- Enable/disable SNS
- AWS Region
- SNS Topic ARN
- AWS Access Key ID
- AWS Secret Access Key

All fields are validated and translated (English/Spanish). If SNS is enabled, all required fields must be set and the Topic ARN must start with `arn:aws:sns:`.

**UCI Configuration Example (manual):**
```sh
uci set yunbridge.main.sns_enabled='1'  # 1 to enable SNS, 0 to disable
uci set yunbridge.main.sns_region='us-east-1'
uci set yunbridge.main.sns_topic_arn='arn:aws:sns:us-east-1:123456789012:YourTopic'
uci set yunbridge.main.sns_access_key='AKIA...'
uci set yunbridge.main.sns_secret_key='...'
uci commit yunbridge
/etc/init.d/yunbridge restart
```

**How it works:**
- When enabled, the daemon will publish messages to the configured SNS topic in parallel with MQTT.
- All pin control, mailbox, and command flows are supported via SNS.

**Typical SNS Usage:**
- Publish a message to the SNS topic to control a pin:
  - Data: `PIN13 ON` or `PIN13 OFF`
- Subscribe to the SNS topic (via SQS, Lambda, or other AWS service) to receive state updates and mailbox messages.

**Hybrid Architecture:**
You can use MQTT and SNS al mismo tiempo. Esto permite integración local y en la nube para IoT, automatización y control remoto.

The daemon supports Google Pub/Sub in addition to MQTT. You can enable Pub/Sub messaging for cloud integration and hybrid workflows.


**Configuration via LuCI Web UI:**
You can configure all Pub/Sub options directly from the YunBridge LuCI interface:

- Enable/disable Pub/Sub
- Google Cloud Project ID
- Pub/Sub Topic Name
- Pub/Sub Subscription Name
- Service Account Credentials Path (must be a `.json` file)

All fields are validated and translated (English/Spanish). If Pub/Sub is enabled, all required fields must be set and the credentials file must end in `.json`.

**UCI Configuration Example (manual):**


**How it works:**
- When enabled, the daemon will publish and subscribe to both MQTT and Pub/Sub topics.
- Messages from either system are routed to the main handler (serial, Arduino, etc.).
- All pin control, mailbox, and command flows are supported via Pub/Sub.
- Message deduplication is handled automatically.

**Typical Pub/Sub Usage:**
- Publish a message to the Pub/Sub topic to control a pin:
  - Data: `PIN13 ON` or `PIN13 OFF`
- Subscribe to the Pub/Sub subscription to receive state updates and mailbox messages.

**Hybrid Architecture:**
You can use MQTT, Pub/Sub, or both at the same time. This enables local and cloud integration for IoT, automation, and remote control.

See the code and comments in `bridge_daemon.py` for advanced usage and customization.

### MQTT Security: Authentication and TLS (Optional)

The daemon supports optional MQTT authentication and TLS. You can set these options in UCI or as environment variables:

**UCI Configuration Example:**
```sh
uci set yunbridge.main.mqtt_user='usuario'
uci set yunbridge.main.mqtt_pass='contraseña'
uci set yunbridge.main.mqtt_tls='1'  # 1 to enable TLS, 0 for no TLS
uci set yunbridge.main.mqtt_cafile='/etc/ssl/certs/ca.crt'
uci set yunbridge.main.mqtt_certfile='/etc/ssl/certs/client.crt'
uci set yunbridge.main.mqtt_keyfile='/etc/ssl/private/client.key'
uci commit yunbridge
/etc/init.d/yunbridge restart
```

**Environment Variables (alternative):**
You can also set `MQTT_USER`, `MQTT_PASS`, etc. before starting the daemon (if you adapt the code to read them).

**Notes:**
- If `mqtt_user` and `mqtt_pass` are set, the daemon will use them for MQTT authentication.
- If `mqtt_tls` is set to 1 and certificate paths are provided, the daemon will connect using TLS/SSL.
- All options are optional: if not set, the daemon connects without auth or TLS.

**Example with mosquitto_pub:**
```sh
mosquitto_pub -h <yun-ip> -t yun/pin/13/set -m ON -u usuario -P contraseña --cafile /etc/ssl/certs/ca.crt
```

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
The `openwrt-yun-core/package` directory contains scripts and config files to ensure Bridge v2 and YunBridge v2 work on modern OpenWRT:
- `yunbridge.init`: Init script to start/stop YunBridge daemon
- `99-yunbridge-ttyath0.conf`: UCI config for serial port
- `yunbridge.files`: List of files for package manager


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


**UCI Configuration Management (examples):**
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
- Ensure `/dev/ttyATH0` exists and is not used by other processes.
- Check `/etc/inittab` and `/etc/config/system` for serial port conflicts.
- Use UCI config to adjust baudrate if needed.
After running the script, upload the main sketch `LED13BridgeControl.ino` (at the project root) to your Yun using the Arduino IDE, reboot if needed, and test MQTT/WebUI integration.



## 2. Architecture & Components



The repository is now organized into the following main components:



### Data Flow Between Components

```
  [Python Client Scripts (Plugin System)]
     |         |         |
   [MQTT Plugin] [SNS Plugin]
     |         |         |
     v         v         v
  [MQTT Broker] [Amazon SNS]
     |         |         |
     +---------+---------+
       |
       v
     [YunBridge Daemon (Python)]
      |                |
      v                v
    [Arduino Sketch/Library]   [Web UI (LuCI)]
```

**Explanation:**
- **Python Client Scripts (Plugin System):** All example scripts use a modular plugin system. You can select MQTT, Pub/Sub, or SNS by changing a single line or argument. New plugins can be added for other messaging systems.
- **MQTT/Amazon SNS Plugins:** Each plugin implements a common interface and handles connection, publish, and subscribe logic for its backend.
- **MQTT Broker / SNS:** All three messaging systems can be used in parallel. The daemon routes messages between them and the hardware/software components.
- **YunBridge Daemon:** Subscribes to relevant topics on all enabled systems, translates messages to serial commands for the Arduino, and publishes state or responses back to all systems. Also reads/writes configuration via UCI.
- **Arduino Sketch/Library:** Receives serial commands from the daemon, controls hardware pins, and sends state or mailbox messages back via serial. These are then published to all enabled messaging systems by the daemon.
- **Web UI (LuCI):** Provides a real-time interface for users. It interacts with the daemon (status, logs, config) and can also act as a messaging client for live pin control and monitoring.

**Typical Flow Example:**
- User toggles a pin in the Web UI, or via a Python client script (using any plugin), or via any MQTT/PubSub/SNS client.
- The command is published to the relevant broker/service (MQTT, Pub/Sub, or SNS).
- The daemon receives the message, sends the command over serial to the Arduino.
- The Arduino sets the pin and sends the new state back over serial.
- The daemon publishes the new state to all enabled messaging systems (MQTT, Pub/Sub, SNS).
- The Web UI and any subscribed clients/services see the updated state in real time.


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
  - `MAILBOX <msg>`: (legacy, now migrated to MQTT)
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

## Troubleshooting & Log Examples

If you encounter issues, check the following log files for details:

- `/tmp/yunbridge_daemon.log` (main daemon)
- `/tmp/yunbridge_mqtt_plugin.log` (MQTT plugin)
- `/tmp/yunbridge_sns_plugin.log` (SNS plugin)
- `/tmp/yunbridge_install.log` (installer)

**Example log entries:**

```
2025-09-21 12:34:56,789 INFO yunbridge.daemon: [MQTT] Connected with result code 0
2025-09-21 12:34:57,123 ERROR yunbridge.mqtt_plugin: MQTT connect error: [Errno 111] Connection refused
```

**Common issues:**
- Missing or invalid configuration: check for `Config error:` lines in the logs.
- Permission errors: ensure the user running the scripts has access to serial ports and config files.
- Network errors: verify broker/service addresses and credentials.

**Forcing a rollback:**
To test the installer's rollback, add a line with `false` after any checkpoint in `install.sh`. The script should abort, clean up, and log the error.

## Messaging Backend Exclusivity in LuCI

The LuCI web interface enforces that only one messaging backend (MQTT or Amazon SNS) can be enabled at a time:

- When you enable one backend, the other option is automatically disabled in the UI.
- A visual warning is shown, indicating which option is disabled and why.
- If you attempt to enable more than one backend (e.g., by editing the config file directly), the interface will block saving and display an error message.
- This exclusivity ensures the bridge daemon operates safely and avoids configuration conflicts.

_You will see an informational note in the LuCI UI reminding you of this rule._

## Recommended optimization: Move /tmp to the SD card

To avoid space issues and ensure that temporary files and large logs do not fill up RAM or internal flash, you can move the `/tmp` directory to the SD card using a bind mount.

**Automatic steps:**
- The `install.sh` script already performs this operation automatically if the SD card is mounted (default: `/mnt/sda1`).
- It creates the directory `/mnt/sda1/tmp` and mounts it over `/tmp`.
- The change is made persistent in `/etc/rc.local` so it is applied after every reboot.

**Why is this useful?**
- System and YunBridge logs and temporary files will be stored on the SD card, preventing RAM or flash from filling up.

**Verify with:**
```sh
df -h /tmp
```

If you see that `/tmp` points to the SD card, the optimization is active.

> **Note:**
> - The `/tmp` directory is automatically set up to use the SD card on every boot.
> - The YunBridge daemon and all Python dependencies are installed system-wide, with logs and temporary files stored on the SD card.
> - This ensures maximum reliability and prevents storage issues, but requires the SD card to always be present and mounted at boot.

## Using an External MQTT Broker

> **Important:**
> The MQTT broker does not need to be installed on the Yun/OpenWRT device. It is recommended to use an external MQTT broker (just like Pub/Sub and SNS), either on another computer, a server, or a cloud service.

### How to install Mosquitto MQTT broker on another computer (Linux example)

1. **On your PC, server, or Raspberry Pi:**
   ```sh
   sudo apt update
   sudo apt install mosquitto mosquitto-clients
   sudo systemctl enable mosquitto
   sudo systemctl start mosquitto
   ```

2. **Check that the broker is running:**
   ```sh
   sudo systemctl status mosquitto
   # or
   netstat -tuln | grep 1883
   ```

3. **Configure YunBridge to use the external broker:**
   - In the LuCI Web UI or UCI config, set the `MQTT Host` to the IP address of your external broker (e.g., your PC or server).
   - Example: `192.168.1.100` (replace with your broker's IP).

4. **Test from any client:**
   ```sh
   mosquitto_pub -h <broker-ip> -t yun/pin/13/set -m ON
   mosquitto_sub -h <broker-ip> -t yun/pin/13/state
   ```

**You can also use cloud MQTT services (e.g., HiveMQ, CloudMQTT, AWS IoT Core) as your broker.**

> The YunBridge daemon will connect to the broker you specify, just like with SNS. You do not need to run a broker on the Yun itself.

## Running the Daemon Manually

You can start the daemon manually with:

```sh
/usr/bin/python3 /usr/libexec/yunbridge/bridge_daemon.py
```

Or use the wrapper script (recommended):

```sh
/usr/bin/yunbridge
```

