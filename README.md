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

**Important Note:** The `Bridge` library has been updated to be purely asynchronous. It no longer contains blocking functions. All interactions that expect a response from the Linux side must be done using callbacks (e.g., `requestDigitalRead()` and `onDigitalReadResponse()`).

## Usage

Once everything is installed, you can start interacting with your Yun.

### LuCI Web Interface

Open your browser and navigate to your Yun's IP address. You will find the YunBridge configuration under `Services > YunBridge`. From there, you can configure the MQTT broker, serial port, and other settings.

### Python Client Example

The Python client is a set of conventions on top of MQTT. You can use any MQTT client library to interact with the Yun. Here is an example using `aiomqtt`:

```python
import asyncio
import aiomqtt

async def main():
    async with aiomqtt.Client("your_yun_ip") as client:
        # Turn pin 13 ON
        await client.publish("br/d/13", "1")
        await asyncio.sleep(1)
        # Turn pin 13 OFF
        await client.publish("br/d/13", "0")

if __name__ == "__main__":
    asyncio.run(main())
```

For a more detailed example, see `openwrt-yun-client-python/examples/all_features_test.py`.

### REST API

You can also control pins via the REST API.

**Turn a pin ON:**
```sh
curl -X POST -H "Content-Type: application/json" -d '{"state": "ON"}' http://<your_yun_ip>/cgi-bin/luci/admin/services/yunbridge/api/pin/13
```

**Check pin status:**
The REST API is write-only for simplicity. Pin status should be monitored by subscribing to the appropriate MQTT topic (e.g., `br/d/13/value`).

## Troubleshooting

Here are some common issues and how to resolve them:

### Cannot connect to the Yun via SSH

- **Check the IP address:** Make sure you are using the correct IP address for your Yun. You can find it in your router's DHCP client list.
- **Check the network connection:** Ensure that your computer and the Yun are on the same network.
- **Check the Yun's power:** Make sure the Yun is properly powered.

### The `yunbridge` daemon is not running

- **Check the logs:** SSH into the Yun and check the daemon's log file for errors:
  ```sh
  logread -e yunbridge
  ```
- **Restart the daemon:**
  ```sh
  /etc/init.d/yunbridge restart
  ```

### Serial communication issues

- **Check the baud rate:** The Arduino sketch uses a fixed baud rate of `115200`. Ensure the baud rate configured in the LuCI web interface (`Services > YunBridge`) matches this value.
- **Check the serial port:** The serial port configured in LuCI must be `/dev/ttyATH0`.
- **Check the Arduino sketch:** Make sure you have uploaded a sketch that uses the `Bridge` library and calls `Bridge.begin()` and `Bridge.process()`.

## Architecture and Data Flow

To understand how the ecosystem works, it's helpful to trace the path of a command from start to finish. The `bridge_daemon.py` running on the Linux side acts as the central hub, translating messages between the network (MQTT) and the microcontroller (serial).

### Example 1: Control Flow (Turning an LED ON)

This example shows how an external command reaches the Arduino.

1.  **Initiator (External Client):** A user sends a REST API request to the Yun's LuCI endpoint.
    ```sh
    curl -X POST ... -d '{"state": "ON"}' http://<your_yun_ip>/cgi-bin/luci/admin/services/yunbridge/api/pin/13
    ```
2.  **Web Server (uhttpd on OpenWRT):** The web server receives the request and forwards it to the LuCI framework.
3.  **LuCI Controller (`yunbridge.lua`):** The Lua controller script handles the API request. It parses the pin and state, and then executes the `mosquitto_pub` command-line tool to publish an MQTT message.
    -   **Topic:** `br/d/13`
    -   **Payload:** `1`
4.  **MQTT Broker:** The broker receives the message and forwards it to all subscribed clients.
5.  **Bridge Daemon (`bridge_daemon.py`):** The daemon is subscribed to `br/d/#`. It receives the message on `br/d/13`.
6.  **Serial Protocol Translation:** The daemon translates the MQTT message into a binary RPC frame.
    -   **Command:** `CMD_DIGITAL_WRITE` (0x11)
    -   **Payload:** `[pin=13, value=1]`
    -   It then wraps this in a frame with a header and CRC, encodes it using COBS, and sends it over the serial port (`/dev/ttyATH0`).
7.  **Arduino Microcontroller (`BridgeControl.ino`):**
    -   The `Bridge.process()` function in the main `loop()` reads the serial data.
    -   The `Bridge` library decodes the COBS packet, verifies the CRC, and parses the frame.
    -   The library identifies the `CMD_DIGITAL_WRITE` command and automatically calls the standard Arduino function `digitalWrite(13, HIGH)`.

### Example 2: Data Reading Flow (Reading a Sensor)

This example shows how data from the Arduino is sent to an external client.

1.  **Initiator (External Client):** A Python script wants to read the value of pin 13.
2.  **MQTT Publication:** The script publishes an empty message to a specific MQTT topic to trigger a read.
    -   **Topic:** `br/d/13/read`
    -   **Payload:** (empty)
3.  **Bridge Daemon (`bridge_daemon.py`):** The daemon receives this message.
4.  **Serial Protocol Translation:** The daemon creates and sends a `CMD_DIGITAL_READ` (0x13) frame to the Arduino with the pin number in the payload.
5.  **Arduino Microcontroller (`BridgeControl.ino`):**
    -   The `Bridge` library receives the `CMD_DIGITAL_READ` frame.
    -   It automatically calls the standard Arduino function `digitalRead(13)` to get the value.
    -   The library then constructs a **response frame**, `CMD_DIGITAL_READ_RESP` (0x15), containing the pin number and its value.
    -   This response frame is encoded and sent back over the serial port to the Linux side.
6.  **Bridge Daemon (`bridge_daemon.py`):** The daemon's serial reader task receives the response frame, decodes it, and verifies it.
7.  **MQTT Translation:** The daemon parses the response and publishes the value to a different MQTT topic.
    -   **Topic:** `br/d/13/value`
    -   **Payload:** `0` or `1`
8.  **Final Client (External Script):** The Python script, which was subscribed to `br/d/13/value`, receives the message with the pin's current state.

This decoupled architecture using MQTT as an intermediary makes the system extremely flexible and robust.

## Low-Level Communication Protocol

While MQTT is used for external communication, the core bridge between the Linux processor and the Arduino microcontroller relies on a custom, high-performance binary RPC protocol over the serial port. This protocol is designed for reliability and efficiency, incorporating:

-   **Binary Framing:** A well-defined frame structure with a header, payload, and checksum.
-   **COBS Encoding:** Consistent Overhead Byte Stuffing ensures that packet boundaries are reliably detected using `0x00` bytes.
-   **CRC Checksum:** A CRC-16-CCITT checksum is used to guarantee data integrity and detect corruption.

For a complete technical specification of the serial protocol, including frame structure, command IDs, and payload definitions, please see [**PROTOCOL.md**](./PROTOCOL.md).

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
- **Core System:**
    - Support for new OpenWRT targets.
    - Expanded documentation and tutorials.
- **Web UI:**
    - Advanced dashboard with real-time visualizations.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3). See the `LICENSE` file for details.