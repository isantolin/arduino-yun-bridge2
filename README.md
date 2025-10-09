# Arduino Yun Bridge v2

A complete, modern, and open-source software ecosystem to revitalize the Arduino Yun in 2025 and beyond. This project replaces the original software stack with a robust, modular, and extensible platform for bridging Arduino microcontrollers with OpenWRT-based Linux systems.

## Overview

The Arduino Yun Bridge v2 is designed to provide a fully functional and up-to-date software stack for the Arduino Yun. It enables seamless communication between the Arduino microcontroller and the onboard Linux system using modern protocols like MQTT. The entire system is modular, allowing for easy extension and maintenance.

## Features

- **Modern Communication:** Uses MQTT as the primary communication protocol between the Linux side and any client, with a robust serial protocol for the microcontroller.
- **Modular Architecture:**
    - A core Python daemon (`bridge_daemon.py`) that bridges MQTT and serial communication.
    - A C++ library for Arduino sketches.
    - A LuCI web interface for configuration and real-time control.
    - A RESTful API for pin control.
- **Extensible Python Client:** A Python library with a plugin system to easily interact with the bridge.
- **Easy Installation:** A set of scripts to automate the compilation, device setup, and installation process.
- **Web UI:** A simple web UI for real-time pin control and status monitoring.
- **Comprehensive Examples:** Includes example Arduino sketches and Python scripts for all major features.

## Project Structure

The project is divided into several components:

- `openwrt-yun-bridge/`: The core Python daemon that runs on the Yun.
- `openwrt-library-arduino/`: The C++ library for Arduino sketches.
- `openwrt-yun-client-python/`: The Python client library and examples.
- `luci-app-yunbridge/`: The LuCI web interface for OpenWRT.
- `openwrt-yun-core/`: Core scripts and configuration files for OpenWRT.
- `1. compile.sh`: Script to compile all the necessary packages.
- `2. expand.sh`: Script to prepare the SD card on the Yun.
- `3. install.sh`: Script to install the ecosystem on the Yun.

## Getting Started: Installation

Follow these steps to get the Arduino Yun Bridge v2 ecosystem up and running.

### Prerequisites

- An Arduino Yun.
- A microSD card (at least 2GB, 4GB or more recommended).
- A working OpenWRT installation on the Yun.
- A Linux-based development machine to compile the packages.

### Step 1: Compile the Packages (on your Development Machine)

First, you need to compile the OpenWRT packages (`.ipk` files) and the Python client library (`.whl` file).

1.  Clone this repository on your development machine.
2.  Run the compilation script:
    ```sh
    ./1. compile.sh
    ```
    This script will download the OpenWRT SDK, all necessary dependencies, and compile the packages. The final artifacts will be placed in the `bin/` directory.

### Step 2: Prepare the Arduino Yun (on the Yun)

For the ecosystem to work correctly, you need to expand the Yun's storage using a microSD card.

1.  Insert the microSD card into the Yun.
2.  Copy the `2. expand.sh` script to your Yun:
    ```sh
    scp ./2. expand.sh root@<your_yun_ip>:/root/
    ```
3.  SSH into your Yun and run the script. This will format the SD card, set it up as the new root filesystem (`extroot`), and create a swap file.
    ```sh
    ssh root@<your_yun_ip>
    chmod +x /root/2. expand.sh
    /root/2. expand.sh
    ```
    The Yun will reboot after the process is complete.

### Step 3: Install the Ecosystem (on the Yun)

After the Yun has rebooted and is running from the SD card, you can install the ecosystem.

1.  Copy the compiled packages from your development machine to the Yun. The `3. install.sh` script and the `bin/` directory are needed.
    ```sh
    scp ./3. install.sh root@<your_yun_ip>:/root/
    scp -r ./bin root@<your_yun_ip>:/root/
    ```
2.  SSH into your Yun and run the installation script:
    ```sh
    ssh root@<your_yun_ip>
    chmod +x /root/3. install.sh
    /root/3. install.sh
    ```
    This script will install all `.ipk` packages, Python dependencies, configure the system, and start the `yunbridge` daemon.

### Step 4: Arduino Setup

1.  Install the Arduino library. On your development machine, run the `install.sh` script inside the `openwrt-library-arduino` directory. This will copy the library to your Arduino IDE's libraries folder.
    ```sh
    cd openwrt-library-arduino
    ./install.sh
    ```
2.  Open the Arduino IDE, and you will find the library in the examples menu.
3.  Upload a sketch from the examples to your Arduino Yun.

## Usage

Once everything is installed, you can start interacting with your Yun.

### LuCI Web Interface

Open your browser and navigate to your Yun's IP address. You will find the YunBridge configuration under `Services > YunBridge`. From there, you can configure the MQTT broker, serial port, and other settings.

### Python Client Example

Here is a simple example of how to control pin 13 using the Python client library:

```python
from yunbridge_client.plugin_loader import PluginLoader

# Load the MQTT plugin
plugin = PluginLoader.load_plugin('mqtt_plugin')('localhost', 1883)

plugin.connect()
plugin.publish('yun/pin/13/set', 'ON')
plugin.disconnect()
```

### REST API

You can also control pins via the REST API.

**Turn a pin ON:**
```sh
curl -X POST -H "Content-Type: application/json" -d '{"state": "ON"}' http://<your_yun_ip>/arduino-webui-v2/pin/13
```

**Check pin status:**
```sh
curl -X GET http://<your_yun_ip>/arduino-webui-v2/pin/13
```

## MQTT Topics and Data Flow

The bridge uses MQTT to expose the Arduino's functionalities and to control the Linux environment on the Yun. All topics are prefixed with `br/`.

### Arduino Interaction Topics

These topics are for direct interaction with the Arduino microcontroller.

| Topic | Direction | Description | Data Flow |
| --- | --- | --- | --- |
| `br/d/{pin}` | MQTT -> Arduino | **Digital Write:** Sets a digital pin to HIGH (1) or LOW (0). | An MQTT client publishes `1` or `0`. The daemon sends a `CMD_DIGITAL_WRITE` command to the Arduino via serial. |
| `br/a/{pin}` | MQTT -> Arduino | **Analog Write:** Sets an analog pin to a specific value (0-255). | An MQTT client publishes a value. The daemon sends a `CMD_ANALOG_WRITE` command to the Arduino. |
| `br/d/{pin}/mode` | MQTT -> Arduino | **Set Pin Mode:** Configures a pin as INPUT, OUTPUT, or INPUT_PULLUP. | An MQTT client publishes `0` (INPUT), `1` (OUTPUT), or `2` (INPUT_PULLUP). The daemon sends a `CMD_SET_PIN_MODE` command. |
| `br/d/{pin}/read` | MQTT -> Arduino | **Digital Read:** Triggers a read from a digital pin. | An MQTT client publishes an empty message. The daemon sends a `CMD_DIGITAL_READ` command. The Arduino responds with the value. |
| `br/a/{pin}/read` | MQTT -> Arduino | **Analog Read:** Triggers a read from an analog pin. | An MQTT client publishes an empty message. The daemon sends a `CMD_ANALOG_READ` command. The Arduino responds with the value. |
| `br/d/{pin}/value` | Arduino -> MQTT | **Digital Value:** Publishes the value of a digital pin. | The Arduino sends a `CMD_DIGITAL_READ_RESP` frame. The daemon receives it and publishes the value to this topic. |
| `br/a/{pin}/value` | Arduino -> MQTT | **Analog Value:** Publishes the value of an analog pin. | The Arduino sends a `CMD_ANALOG_READ_RESP` frame. The daemon receives it and publishes the value to this topic. |
| `br/console/in` | MQTT -> Arduino | **Console Input:** Sends a string to the Arduino's console. | An MQTT client publishes a message. The daemon sends a `CMD_CONSOLE_WRITE` command to the Arduino. |
| `br/console/out` | Arduino -> MQTT | **Console Output:** Publishes messages from the Arduino's console. | The Arduino sends a `CMD_CONSOLE_WRITE` frame. The daemon receives it and publishes the payload to this topic. |
| `br/mailbox/write` | MQTT -> Mailbox | **Write to Mailbox:** Queues a message on the Linux side for the Arduino to read. | An MQTT client publishes a message. The daemon adds it to a queue. |
| `br/mailbox/available`| Mailbox -> MQTT | **Mailbox Messages:** Publishes the number of messages waiting in the mailbox. | Published by the daemon whenever a message is added via `br/mailbox/write`. |
| `br/mailbox/processed`| Arduino -> MQTT | **Mailbox Processed:** Publishes a message that the Arduino has processed and sent back. | The Arduino sends a `CMD_MAILBOX_PROCESSED` frame. The daemon publishes the payload to this topic. |

### MQTT-Only Interaction Topics

These topics are handled entirely by the `bridge_daemon.py` on the Linux side and do not involve the Arduino.

| Topic | Direction | Description | Data Flow |
| --- | --- | --- | --- |
| `br/sh/run` | MQTT -> Linux | **Run Shell Command:** Executes a shell command on the Yun's Linux system. | An MQTT client publishes a command string. The daemon executes it using `subprocess.run`. |
| `br/sh/response` | Linux -> MQTT | **Shell Command Response:** Publishes the `stdout` and `stderr` of the executed command. | After the command from `br/sh/run` completes, the daemon publishes its output to this topic. |
| `br/datastore/put/{key}` | MQTT -> Linux | **DataStore Put:** Stores a value in the daemon's in-memory key-value store. | An MQTT client publishes a value to a specific key sub-topic. The daemon stores it in a Python dictionary. |
| `br/datastore/get/{key}` | Linux -> MQTT | **DataStore Get:** Publishes the value of a key for state synchronization. | When a key is updated via `br/datastore/put`, the daemon immediately publishes the new value here. |

## Roadmap

- **MQTT:**
    - Advanced control features.
    - Certificate support for secure connections.
    - WebSockets support.
- **Communication Protocols:**
    - Implementation of COBS (Consistent Overhead Byte Stuffing) for more reliable serial communication.
- **Core System:**
    - Support for new OpenWRT targets.
    - Expanded documentation and tutorials.
- **Web UI:**
    - Advanced dashboard with real-time visualizations.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3). See the `LICENSE` file for details.