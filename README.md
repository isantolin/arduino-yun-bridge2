# Arduino Yun v2 Ecosystem (Unified Documentation)

## Prerequisites (Mandatory)

**You must complete these steps before installing or using this ecosystem:**

1. **Update your Arduino Yun to the latest OpenWRT version:**
   - Follow the official instructions here: https://openwrt.org/toh/arduino.cc/yun
   - _This is a mandatory requirement. The bridge and all scripts expect a modern, up-to-date OpenWRT system._

2. **Expand storage using a microSD card (extroot):**
   - Insert a microSD card and follow the official OpenWRT extroot guide: https://openwrt.org/docs/guide-user/additional-software/extroot_configuration
   - _This is also mandatory. The Yun's internal storage is insufficient for Python, pip, and cloud libraries._

3. **(Recommended) Enable swap:**
   - During the extroot process, it is highly recommended to activate swap on the SD card. This improves stability when installing packages and running Python/cloud services.

---

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



## Python Client Plugin System

The `openwrt-yun-client-python` directory contains example scripts and a modular plugin system for interacting with the YunBridge ecosystem using different messaging backends (MQTT, Google Pub/Sub, Amazon SNS, etc.).

### Plugin System Overview

- All messaging systems are implemented as plugins in `openwrt-yun-client-python/yunbridge_client/`.
- Each plugin implements a common interface (`plugin_base.py`):
  - `connect()`
  - `publish(topic, message)`
  - `subscribe(topic, callback)`
  - `disconnect()`
- Plugins available:
  - `mqtt_plugin.py` (MQTT via paho-mqtt)
  - `pubsub_plugin.py` (Google Pub/Sub)
  - `sns_plugin.py` (Amazon SNS)
- New messaging systems can be added as plugins by following the same interface.

### Example Scripts

- `led13_test.py`: Unified example, select backend via argument (`mqtt_plugin`, `pubsub_plugin`, `sns_plugin`).
- `all_features_test.py`: Demonstrates all YunBridge features using the plugin system (MQTT backend by default).
- `*_mqtt_test.py`: Legacy examples, now refactored to use the plugin system (MQTT only, but can be switched as shown below).

### Usage

#### Unified Example (Recommended)

```sh
python3 openwrt-yun-client-python/led13_test.py mqtt_plugin
python3 openwrt-yun-client-python/led13_test.py pubsub_plugin
python3 openwrt-yun-client-python/led13_test.py sns_plugin
```

Edit the config dictionary in `led13_test.py` for your broker/service details.

#### Using SNS and PubSub Plugins in Examples

All example scripts (`*_mqtt_test.py`) are written for MQTT by default, but you can easily switch to SNS or PubSub by uncommenting the relevant code blocks:

```python
# Example: SNS plugin (uncomment to use)
SNS_CONFIG = dict(region='us-east-1', topic_arn='arn:aws:sns:us-east-1:123456789012:YourTopic', access_key='AKIA...', secret_key='...')
PluginClass = PluginLoader.load_plugin('sns_plugin')
plugin = PluginClass(**SNS_CONFIG)
```

```python
# Example: PubSub plugin (uncomment to use)
PUBSUB_CONFIG = dict(project_id='your-gcp-project', topic_name='your-topic', subscription_name='your-sub', credentials_path='/path/to/creds.json')
PluginClass = PluginLoader.load_plugin('pubsub_plugin')
plugin = PluginClass(**PUBSUB_CONFIG)
```

Just comment out the MQTT section and uncomment the SNS or PubSub section as needed. Make sure to fill in your credentials and topic details.

#### All Features Example

```sh
python3 openwrt-yun-client-python/all_features_test.py
```

#### Add a New Plugin

1. Create a new file in `openwrt-yun-client-python/yunbridge_client/` (e.g., `mycloud_plugin.py`).
2. Inherit from `MessagingPluginBase` and implement the required methods.
3. Use `PluginLoader.load_plugin('mycloud_plugin')` in your script.

#### Requirements

- Python 3.7+
- Install dependencies as needed:
  - `pip install paho-mqtt google-cloud-pubsub boto3`

#### Directory Structure

```
openwrt-yun-client-python/
  led13_test.py
  all_features_test.py
  ...
  yunbridge_client/
    plugin_base.py
    plugin_loader.py
    mqtt_plugin.py
    pubsub_plugin.py
    sns_plugin.py
```

git clone https://github.com/isantolin/arduino-yun-bridge2.git
cd arduino-yun-bridge2
sh install.sh

## 1. Installation & Dependencies

### Robust Installer, Logging, and Validation

- The installer (`install.sh`) now features atomic checkpoints and rollback: if any step fails, all changes are reverted and a clear error is logged in `/tmp/yunbridge_install.log`.
- All Python plugins and the main daemon use rotating log files for robust, persistent logging. Log levels are configurable in code.
- All scripts and plugins validate critical configuration parameters before running, and will fail fast with a clear error if any required value is missing.

**Testing rollback:** You can force a failure in `install.sh` (e.g., by adding `false` after a checkpoint) to verify that rollback and cleanup work as expected.

**Log files:**
- `/tmp/yunbridge_daemon.log` (daemon)
- `/tmp/yunbridge_mqtt_plugin.log`, `/tmp/yunbridge_pubsub_plugin.log`, `/tmp/yunbridge_sns_plugin.log` (plugins)

**Configuration validation:**
- The daemon and plugins will not start if required config values are missing or invalid. Errors are logged and shown in the console.



### Amazon SNS Support (Optional)

The daemon supports Amazon SNS in addition to MQTT and Pub/Sub. You can enable SNS messaging for cloud integration and hybrid workflows.

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
- When enabled, the daemon will publish messages to the configured SNS topic in parallel with MQTT and Pub/Sub.
- All pin control, mailbox, and command flows are supported via SNS.

**Typical SNS Usage:**
- Publish a message to the SNS topic to control a pin:
  - Data: `PIN13 ON` or `PIN13 OFF`
- Subscribe to the SNS topic (via SQS, Lambda, or other AWS service) to receive state updates and mailbox messages.

**Hybrid Architecture:**
You can use MQTT, Pub/Sub, and SNS at the same time. This enables local and cloud integration for IoT, automation, and remote control.

The daemon supports Google Pub/Sub in addition to MQTT. You can enable Pub/Sub messaging for cloud integration and hybrid workflows.

**Requirements:**
- Python package: `google-cloud-pubsub` (install with `pip install google-cloud-pubsub`)
- Google Cloud project and Pub/Sub topics/subscriptions
- Service account credentials JSON file

**Configuration via LuCI Web UI:**
You can configure all Pub/Sub options directly from the YunBridge LuCI interface:

- Enable/disable Pub/Sub
- Google Cloud Project ID
- Pub/Sub Topic Name
- Pub/Sub Subscription Name
- Service Account Credentials Path (must be a `.json` file)

All fields are validated and translated (English/Spanish). If Pub/Sub is enabled, all required fields must be set and the credentials file must end in `.json`.

**UCI Configuration Example (manual):**
```sh
uci set yunbridge.main.pubsub_enabled='1'  # 1 to enable Pub/Sub, 0 to disable
uci set yunbridge.main.pubsub_project='your-gcp-project-id'
uci set yunbridge.main.pubsub_topic='your-topic-name'
uci set yunbridge.main.pubsub_subscription='your-subscription-name'
uci set yunbridge.main.pubsub_credentials='/etc/yunbridge/gcp-service-account.json'
uci commit yunbridge
/etc/init.d/yunbridge restart
```

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
   [MQTT Plugin] [PubSub Plugin] [SNS Plugin]
     |         |         |
     v         v         v
  [MQTT Broker] [Google Pub/Sub] [Amazon SNS]
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
- **MQTT/Google PubSub/Amazon SNS Plugins:** Each plugin implements a common interface and handles connection, publish, and subscribe logic for its backend.
- **MQTT Broker / Pub/Sub / SNS:** All three messaging systems can be used in parallel. The daemon routes messages between them and the hardware/software components.
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
- `/tmp/yunbridge_pubsub_plugin.log` (Pub/Sub plugin)
- `/tmp/yunbridge_sns_plugin.log` (SNS plugin)
- `/tmp/yunbridge_install.log` (installer)

**Example log entries:**

```
2025-09-21 12:34:56,789 INFO yunbridge.daemon: [MQTT] Connected with result code 0
2025-09-21 12:34:57,123 ERROR yunbridge.mqtt_plugin: MQTT connect error: [Errno 111] Connection refused
2025-09-21 12:35:01,456 WARNING yunbridge.sns_plugin: SNS subscribe not supported in client. Use SQS or Lambda.
```

**Common issues:**
- Missing or invalid configuration: check for `Config error:` lines in the logs.
- Permission errors: ensure the user running the scripts has access to serial ports and config files.
- Network errors: verify broker/service addresses and credentials.

**Forcing a rollback:**
To test the installer's rollback, add a line with `false` after any checkpoint in `install.sh`. The script should abort, clean up, and log the error.

## Messaging Backend Exclusivity in LuCI

The LuCI web interface enforces that only one messaging backend (MQTT, Google Pub/Sub, or Amazon SNS) can be enabled at a time:

- When you enable one backend, the other options are automatically disabled in the UI.
- A visual warning is shown, indicating which options are disabled and why.
- If you attempt to enable more than one backend (e.g., by editing the config file directly), the interface will block saving and display an error message.
- This exclusivity ensures the bridge daemon operates safely and avoids configuration conflicts.

_You will see an informational note in the LuCI UI reminding you of this rule._

