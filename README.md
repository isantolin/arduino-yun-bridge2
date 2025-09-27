# Arduino Yun Ecosystem v2


## Overview


**Arduino Yun Ecosystem v2** is a modular, open-source platform for bridging Arduino microcontrollers with OpenWRT-based Linux systems. The main goal of this project is to provide a fully functional, modern software stack for the Arduino Yun board in 2025, ensuring its usability with up-to-date tools and protocols. The project is also designed with the intention to extend support in the future to other boards that integrate a microcontroller and Linux system (e.g., ATmega32U4 + Atheros AR9331 in the case of Arduino Yun).

It provides robust, extensible communication between the microcontroller (via serial) and Linux (via MQTT, Python, web interfaces, and a REST API), enabling advanced IoT, automation, and device management scenarios.

---

## Features

- **Modular Architecture:**
  - Core Python daemon bridges MQTT and serial communication.
  - Arduino C++ library for seamless integration with sketches.
  - LuCI web interface for configuration and real-time control.
  - REST Web API (CGI) for pin control and status via HTTP.
  - Plugin system for messaging backends (MQTT, extensible).
- **Communication Protocol:**
  - Serial protocol: Commands like `PIN<N> ON/OFF`, `MAILBOX <msg>`, `SET <key> <val>`, `GET <key>`, `RUN <cmd>`, `WRITEFILE <path> <data>`, `READFILE <path>`.
  - MQTT topics: `yun/pin/<N>/set`, `yun/pin/<N>/state`, `yun/command`, `yun/mailbox/send`, `yun/mailbox/recv`.
  - **All MQTT publish and subscribe operations use QoS 2 (exactly once delivery) for maximum reliability.**
### MQTT Quality of Service (QoS)

All MQTT communication in this ecosystem (daemon, Python client, and test examples) uses **QoS 2** (exactly once delivery) for both publish and subscribe operations. This ensures:

- No message loss or duplication, even in the case of network interruptions.
- Maximum reliability for all device control and monitoring actions.

If you use your own MQTT client, make sure to set `qos=2` in both `publish` and `subscribe` calls for full compatibility.
  - Web REST API for pin control and status (JSON responses).
  - WebSocket support for browser-based MQTT control.
- **Web UI:**
  - Real-time pin control and status monitoring.
  - Log and status visualization.
  - User authentication for MQTT.
- **Extensive Logging:**
  - Rotating logs for daemon, MQTT plugin, and scripts.
  - Status file for external monitoring.
- **Robust Installer:**
  - Atomic install with rollback, swap file management, and dependency checks.
- **Examples and Tests:**
  - Arduino sketches for pin, file, KV store, process, and mailbox features.
  - Python tests for all features using MQTT backend.

---

## Project Structure

- `openwrt-yun-core/`: Core scripts, configs, and serial helpers for OpenWRT.
- `openwrt-yun-bridge/`: Python daemon (`bridge_daemon.py`) for MQTT <-> Serial bridging.
- `openwrt-yun-client-python/`: Python client library and plugin system.
- `openwrt-library-arduino/`: Arduino C++ Bridge library.
- `luci-app-yunbridge/`: LuCI web interface (config, status, web UI).
- `openwrt-yun-client-sketches/`: Example Arduino sketches.

---

## Communication Protocol



### Web REST API for Pin Control (RESTful)

The system provides a RESTful HTTP API for pin control and status:

- **Get pin status:**
  - `GET /arduino-webui-v2/pin/<N>`
  - Example:
    ```sh
    curl -X GET http://<yun_ip>/arduino-webui-v2/pin/13
    ```
  - Response (JSON):
    ```json
    { "status": "ok", "pin": 13, "state": "ON", "message": "Pin 13 is ON" }
    ```

- **Set pin state:**
  - `POST /arduino-webui-v2/pin/<N>`
  - Body (JSON): `{ "state": "ON" }` o `{ "state": "OFF" }`
  - Example:
    ```sh
    curl -X POST -H "Content-Type: application/json" -d '{"state": "ON"}' http://<yun_ip>/arduino-webui-v2/pin/13
    ```
  - Response (JSON):
    ```json
    { "status": "ok", "pin": 13, "state": "ON", "message": "Pin 13 turned ON" }
    ```


- **Errors:**
  - Error responses use the field `status: "error"` and a message, and the appropriate HTTP status code (400, 405, 500, etc).

**Notes:**
- The pin number is specified in the URL (`/pin/<N>`).
- Only HTTP GET and POST methods are accepted.
- The POST body must be valid JSON.

---

---

### Serial Commands (from Linux to Arduino)
* `PIN<N> ON` / `PIN<N> OFF`: Set digital pin state.
* `PIN<N> STATUS`: Query digital pin state (used by CGI endpoint).
* `MAILBOX <msg>`: Send message to Arduino mailbox.
* `SET <key> <val>` / `GET <key>`: Key-value store operations.
* `RUN <cmd>`: Execute Linux command, return output.
* `WRITEFILE <path> <data>` / `READFILE <path>`: File I/O.
* `CONSOLE <msg>`: Console message.

### Serial Responses (from Arduino to Linux)
- `PIN<N> STATE ON/OFF`: Pin state report.
- `VALUE <key> <val>`: KV store response.
- `RUNOUT <output>`: Command output.
- `FILEDATA <data>`: File read result.
- `OK <cmd>` / `ERR <cmd>`: Operation status.

### MQTT Topics
- `yun/pin/<N>/set`: Set pin state (payload: `ON`/`OFF`).
- `yun/pin/<N>/state`: Pin state report.
- `yun/command`: Generic commands (SET, GET, RUN, etc).
- `yun/mailbox/send` / `yun/mailbox/recv`: Mailbox messaging.

---

## Installation



### Requirements
- OpenWRT 22.x or newer (tested on ath79/generic)
- Python 3.7+
- `python3-pyserial`, `python3-paho-mqtt`
- LuCI web interface (for web UI)
- MQTT broker (e.g., Mosquitto)
- **Arduino Yun** (currently only works with Arduino Yun)
- Micro SD card (at least 2 GB)

### Pre-compilation Steps
1. **Update OpenWRT image to the latest version.**
  - Download and flash the latest OpenWRT firmware for your device from https://openwrt.org/.
2. **Expand storage using extroot (recommended for space-constrained devices):**
  - Follow the official OpenWRT guide: https://openwrt.org/docs/guide-user/additional-software/extroot_configuration
  - This allows you to use the Micro SD card as root filesystem, providing more space for packages and logs.

### Steps
1. **Compile all packages:**
   ```sh
   ./compile.sh
   ```
   - Produces `.ipk` (OpenWRT) and `.whl` (Python) in `bin/`.
2. **Install on OpenWRT device:**
   ```sh
   scp bin/*.ipk root@<yun_ip>:/tmp/
   ssh root@<yun_ip>
   ./install.sh
   ```
   - Installs all dependencies, configures swap, and starts the daemon.
3. **Install Arduino library:**
   ```sh
   cd openwrt-library-arduino
   ./install.sh
   ```
   - Installs Bridge library to Arduino IDE.
4. **Upload example sketches:**
   - Use Arduino IDE to upload from `openwrt-yun-client-sketches/`.
5. **Configure via LuCI:**
   - Access LuCI at `http://<yun_ip>/cgi-bin/luci/admin/services/yunbridge`.
   - Set MQTT broker, serial port, and debug options.

---

## Compilation

- **OpenWRT SDK:**
  - The `compile.sh` script downloads and configures the OpenWRT SDK, builds all packages, and copies artifacts to `bin/`.
- **Python Client:**
  - Built as a wheel (`.whl`) using `make wheel` in `openwrt-yun-client-python/`.
- **Arduino Library:**
  - Simple copy to Arduino libraries folder.

---

## Usage Examples

### Arduino Sketch: Pin Control
```cpp
#include <Bridge.h>
void setup() {
  Bridge.begin();
  Bridge.pinOn(13);
}
void loop() {}
```

### Python: Pin Control via MQTT
```python
from yunbridge_client.plugin_loader import PluginLoader
plugin = PluginLoader.load_plugin('mqtt_plugin')('localhost', 1883)
plugin.connect()
plugin.publish('yun/pin/13/set', 'ON')
plugin.disconnect()
```

---

## Technical Notes
- All configuration is managed via UCI and LuCI for OpenWRT.
- Daemon and plugins use rotating logs in `/tmp/` for diagnostics.
- Web UI uses MQTT over WebSockets for real-time control.
- Serial protocol is tolerant to glued/concatenated commands.
- Swap file is managed automatically for low-memory devices.

---


## Roadmap

### MQTT
- Advanced control features
- Certificate support for secure connections
- WebSockets support (outside Arduino)

### Communication Protocols
- Implementation of COBS (Consistent Overhead Byte Stuffing) between OpenWRT and the microcontroller

### Core Yun/OpenWRT
- Support for new OpenWRT targets
- OTA (Over-The-Air) updates for firmware and packages
- Integration of community contributions
- Expanded documentation and tutorials
- Official Mosquitto support with WebSockets on OpenWRT

### Web UI (luci-app-yunbridge)
- Advanced dashboard (MQTT only)
- Usability and real-time visualization improvements
- Integration of plugins and custom panels

---

See `ROADMAP.md` for planned features and contributions.

---

## License
MIT License. Contributions welcome.
