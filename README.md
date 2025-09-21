
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

The `openwrt-yun-core/package` directory contains scripts and config files to ensure Bridge v2 and YunBridge v2 work on modern OpenWRT:

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

After running the script, upload the main sketch `LED13BridgeControl.ino` (at the project root) to your Yun using the Arduino IDE, reboot if needed, and test MQTT/WebUI integration.



## 2. Architecture & Components

El repositorio ahora está organizado en los siguientes componentes principales:

-- **Core Yun/OpenWRT (openwrt-yun-core):**
  - Scripts, configuraciones e integración para el core de Arduino Yun/OpenWRT.
  - Incluye daemon, scripts de arranque, configuración UCI, integración con el sistema.
  - No incluye LuCI ni Web UI.

- **Arduino Library (openwrt-library-arduino):**
  - Biblioteca Bridge v2 para Arduino Yun.
  - Instala la librería en tu IDE usando `openwrt-library-arduino/install.sh`.
  - Ejemplos de sketches en `openwrt-yun-client-sketches/examples/`.

- **Python MQTT Examples (openwrt-yun-client-python):**
  - Scripts de ejemplo para controlar el Yun vía MQTT.
  - Todos los ejemplos están en `openwrt-yun-client-python/`.

- **Arduino Sketch Examples (openwrt-yun-client-sketches/examples):**
  - Ejemplos de sketches para pruebas y validación.
  - Todos los sketches de ejemplo están en `openwrt-yun-client-sketches/examples/`.

- **LuCI App (luci-app-yunbridge):**
  - Paquete independiente para LuCI (Web UI) y configuración avanzada.
  - Todo el código y archivos de LuCI están en `/luci-app-yunbridge`.
  - Instalación y mantenimiento por separado.

### Instalación del Core (openwrt-yun-core)
Sigue las instrucciones de `install.sh` para instalar el core, daemon y scripts.


### Instalación de la Web UI (luci-app-yunbridge)

**Opción recomendada: instalar desde paquete .ipk**

1. Compila el paquete `.ipk` de `luci-app-yunbridge` en un buildroot de OpenWRT:
  - Copia la carpeta `luci-app-yunbridge` a `package/` dentro de tu árbol de OpenWRT.
  - Ejecuta:
    ```sh
    make package/luci-app-yunbridge/compile V=s
    ```
  - El archivo `.ipk` aparecerá en `bin/packages/<arch>/luci/`.
2. Copia el `.ipk` a tu Yun/OpenWRT y ejecuta:
  ```sh
  opkg install luci-app-yunbridge_*.ipk
  ```

**Opción manual (solo si no tienes el .ipk):**

Sigue las instrucciones en `/luci-app-yunbridge/README.md` para instalar la interfaz web y configuración avanzada copiando los archivos manualmente.


**Notas:**
- El daemon YunBridge lee la configuración desde UCI (`/etc/config/yunbridge`) usando `python3-uci`. Si una opción no existe, se usa el valor por defecto.
- El paquete LuCI es opcional y puede instalarse/desinstalarse de forma independiente.
- Si el instalador detecta un `.ipk` de `luci-app-yunbridge`, lo instalará automáticamente con `opkg install`. Si no, intentará la instalación manual de archivos.

Todos los ejemplos y scripts legacy han sido eliminados. Solo se soportan flujos MQTT.

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
- Ejemplo actualizado: `openwrt-yun-client-python/mailbox_mqtt_test.py`

Todos los scripts usan los mismos topics y lógica MQTT que el daemon y el código Arduino. Consulta cada script para ejemplos de uso.

#### Architecture Overview
- **MQTT Broker:** Local (OpenWRT/Mosquitto) or external.
- **YunBridge Daemon:** MQTT client, subscribes/controls pin topics, publishes states.
- **Arduino Library:** Recibe comandos MQTT desde Linux, reporta cambios de estado.
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
    - Upload `LED13BridgeControl.ino` (at the project root) to your Yun using the Arduino IDE.
    - Run `openwrt-yun-client-python/led13_mqtt_test.py` on the Yun (SSH):
      ```bash
      python3 openwrt-yun-client-python/led13_mqtt_test.py [PIN]
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
