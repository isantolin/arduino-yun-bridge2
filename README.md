
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

### OpenWRT Integration Details

The `openwrt-yun-v2/package` directory contains scripts and config files to ensure Bridge v2 and YunBridge v2 work on modern OpenWRT:

- `bridge-v2.init`: Init script to start/stop YunBridge daemon
- `99-bridge-ttyath0.conf`: UCI config for serial port
- `bridge-v2.files`: List of files for package manager

**Manual Installation Steps (if needed):**
1. Copy all files to your OpenWRT device in the appropriate locations:
  - `/usr/bin/yunbridge` (daemon)
  - `/etc/init.d/bridge-v2` (init script)
  - `/etc/config/bridge-ttyath0` (serial config)
  - `/www/cgi-bin/led13` (CGI script)
2. Make scripts executable:
  ```sh
  chmod +x /etc/init.d/bridge-v2 /www/cgi-bin/led13
  ```
3. Enable and start the service:
  ```sh
  /etc/init.d/bridge-v2 enable
  /etc/init.d/bridge-v2 start
  ```

**Notes:**
- Ensure `/dev/ttyATH0` exists and is not used by other processes.
- Check `/etc/inittab` and `/etc/config/system` for serial port conflicts.
- Use UCI config to adjust baudrate if needed.

After running the script, upload the example sketch from Bridge-v2 to your Yun using the Arduino IDE, reboot if needed, and test MQTT/WebUI integration.


## 2. Architecture & Components

This repository contains all the components for a modern MQTT-based solution for Arduino Yun v2, including:

- **Bridge-v2**: Arduino library (C++) for Yun, with MQTT support and IoT integration examples. Main example: generic pin control via MQTT (default: pin 13, but any pin can be used).
- **YunBridge-v2**: Python3 daemon for OpenWRT, MQTT client, modular and extensible. Integrates with the MQTT broker and WebUI.
- **openwrt-yun-v2**: OpenWRT integration scripts and automated installation. Ensures all dependencies and configs are set up.
- **Web UI (luci-app-yunbridge)**: LuCI Web panel for configuring and monitoring YunBridge on OpenWrt. 
  - Edit YunBridge configuration (MQTT, serial, debug, etc.)
  - Integrated Web UI for real-time control/monitoring (from YunWebUI)
  - UCI config: `/etc/config/yunbridge`
  - Multi-language support (planned)
  - Daemon status and logs in panel (planned)
  - Advanced parameter validation (planned)

### LuCI Panel Installation & Usage

**Dependencies:**
```sh
opkg update
opkg install luci luci-base lua luci-mod-admin-full luci-lib-nixio luci-lib-ipkg luci-compat python3-uci
```

**Manual Installation:**
1. Run the automatic installation script:
  ```sh
  ./install.sh <ROUTER_IP> [user]
  # Example:
  ./install.sh 192.168.1.1 root
  ```
  This will copy all necessary files and restart LuCI.
2. Alternativamente, instalación manual:
  - `luasrc/controller/yunbridge.lua` → `/usr/lib/lua/luci/controller/`
  - `luasrc/model/cbi/yunbridge.lua` → `/usr/lib/lua/luci/model/cbi/`
  - `luasrc/view/yunbridge/` → `/usr/lib/lua/luci/view/`
  - `root/etc/config/yunbridge` → `/etc/config/yunbridge`
  - `root/www/yunbridge/index.html` → `/www/yunbridge/index.html`
  - Reinicia LuCI: `/etc/init.d/uhttpd restart; /etc/init.d/rpcd restart`

Luego accede a LuCI: Services > YunBridge

**Uso:**
- Instala el paquete o copia los archivos manualmente
- Accede vía LuCI: Services > YunBridge
- Configura parámetros y usa la pestaña Web UI

**Archivos principales:**
- `luasrc/controller/yunbridge.lua`: LuCI controller
- `luasrc/model/cbi/yunbridge.lua`: Config form
- `luasrc/view/yunbridge/webui.htm`: Embedded Web UI
- `root/etc/config/yunbridge`: UCI config defaults
- `root/www/yunbridge/index.html`: YunWebUI frontend (copy from YunWebUI-v2)

**Notas:**
- El daemon YunBridge lee la configuración desde UCI (`/etc/config/yunbridge`) usando `python3-uci`. Si una opción no existe, se usa el valor por defecto.
- WebSocket support is not available in the default OpenWrt Mosquitto package. Use standard MQTT (port 1883) for now.
 
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
  - `MAILBOX <msg>`: (legacy, ahora migrado a MQTT)
    - `RUN <cmd>`: Run a shell command
    - `CONSOLE <msg>`: Print to console

#### Daemon Topic Subscriptions
- Subscribes: `yun/pin/+/set`, `yun/pin/+/get`, `yun/command`, `yun/mailbox/send`
- Publishes: `yun/pin/<N>/state`, responses to `yun/command`, `yun/mailbox/recv`

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


#### Ejemplo de mensajes arbitrarios (nuevo flujo MQTT)
- Para enviar un mensaje al Arduino desde cualquier cliente MQTT:
- Publica el texto en el topic: `yun/mailbox/send`
- El Arduino recibirá el mensaje como `MAILBOX <msg>` por Serial1 y lo mostrará por consola.
- Para que el Arduino envíe un mensaje a otros clientes MQTT:
- El sketch debe enviar por Serial1: `MAILBOX <msg>`
- El daemon publicará ese mensaje en el topic: `yun/mailbox/recv`
- Ejemplo actualizado: `YunBridge-v2/examples/mailbox_mqtt_test.py`

Todos los scripts usan los mismos topics y lógica MQTT que el daemon y el código Arduino. Consulta cada script para ejemplos de uso.

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
- MQTT authentication (username/password) is supported (see config options).
- TLS support is planned (see roadmap).

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
