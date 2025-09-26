# **ARduino Yun Ecosystem**

## **Overview**

It enables seamless communication between the OpenWRT (Linux) side and the ATmega32U4 (Arduino) side, using **MQTT as the exclusive backend**. The system is designed for reliability and ease of use, focusing on modern **Python 3.11+** and **OpenWRT** best practices.

All configuration is handled through **UCI**, and a dedicated **LuCI Web UI** is provided for configuration, status, and log viewing.

## **Key Features**

* **MQTT-Only Backend:** Dedicated communication via MQTT; no legacy gRPC support.  
* **UCI-Based Configuration:** All parameters (MQTT host, credentials, serial port, debug mode) are configured centrally via the OpenWRT **UCI** system, eliminating the need for configuration files or environment variables.  
* **System-Wide Python:** No Python virtual environments are required or used. All Python code runs system-wide, simplifying deployment.  
* **Modular Build & Install:** Utilizes a package-centric build system (compile.sh and install.sh) to install pre-built .ipk (OpenWRT) and .whl (Python) artifacts.  
* **Persistent Swap:** Creates a persistent swap file to ensure reliable installation on low-memory devices.  
* **Robust Logging:** Logs are written to /tmp or the SD card (if optimized), with immediate flushing in debug mode for real-time troubleshooting.

## **Quick Start Guide**

Follow these steps to set up the bridge on your device:

### **1\. Build Packages Locally**

Clone the repository and run the build script. This will generate all necessary artifacts (.ipk and .whl files) into the bin/ directory.

git clone \[https://github.com/isantolin/arduino-yun-bridge2.git\](https://github.com/isantolin/arduino-yun-bridge2.git)  
cd arduino-yun-bridge2  
./compile.sh  
\# All artifacts are generated in bin/

### **2\. Install on OpenWRT Device**

Copy the bin/ directory to your Yun/OpenWRT device and run the installer. This script handles the installation of all pre-built artifacts and system setup (including swap creation and dependencies).

\# On the Yun/OpenWRT device  
sh install.sh

### **3\. Upload Arduino Sketch**

Upload the main sketch to the ATmega32U4 side using the Arduino IDE.

* **Sketch:** LED13BridgeControl.ino

### **4\. Configure and Test**

1. Open the Web UI (LuCI) at: http://\<yun-ip\>/cgi-bin/luci/admin/services/yunbridge  
2. Configure your MQTT parameters and serial port.  
3. **Restart** the daemon to apply changes: /etc/init.d/yunbridge restart  
4. Test pin control via MQTT or the Web UI.

## **Architecture & Data Flow**

The system is organized into distinct components, with all communication routed through the central **MQTT Broker**.

| Component | Side | Description |
| :---- | :---- | :---- |
| **Arduino Sketch/Library** | ATmega32U4 | Controls hardware, handles pin state changes, communicates via serial. |
| **YunBridge Daemon (Python)** | OpenWRT | Primary logic. Reads configuration (UCI), translates MQTT messages to serial commands, and publishes Arduino status back to MQTT. |
| **MQTT Broker** | OpenWRT / External | Central hub for all command and state messages. |
| **Python Client Scripts** | OpenWRT | Example scripts for automation and pin control via MQTT. |
| **Web UI (LuCI)** | OpenWRT | Provides a real-time interface for configuration, status, and pin control. |

**Typical Communication Flow (e.g., toggling a pin):**

1. A command (from the Web UI or an external client) is published to the **MQTT Broker**.  
2. The **YunBridge Daemon** receives the message.  
3. The daemon translates the message and sends it over serial to the **Arduino Sketch**.  
4. The Arduino changes the pin state and reports the new state over serial.  
5. The daemon receives the new state and publishes it back to the **MQTT Broker**.  
6. The **Web UI** and all subscribed clients update in real time.

## **Configuration**

All configuration is persistent and managed through the OpenWRT UCI system. Always commit changes and restart the daemon.

### **UCI Configuration Example**

uci set yunbridge.@bridge\[0\].serial\_port='/dev/ttyATH0'  
uci set yunbridge.@bridge\[0\].mqtt\_host='192.168.1.100'  
uci set yunbridge.@bridge\[0\].mqtt\_user='myuser'  
uci set yunbridge.@bridge\[0\].mqtt\_pass='mypassword'  
uci set yunbridge.@bridge\[0\].debug='1'  \# Enable debug mode  
uci commit yunbridge  
/etc/init.d/yunbridge restart

### **MQTT Security (Authentication & TLS)**

The daemon optionally supports authentication and TLS/SSL connections.

\# Set authentication credentials  
uci set yunbridge.main.mqtt\_user='user'  
uci set yunbridge.main.mqtt\_pass='password'

\# Enable TLS/SSL (optional)  
uci set yunbridge.main.mqtt\_tls='1'  
uci set yunbridge.main.mqtt\_cafile='/etc/ssl/certs/ca.crt'  
uci set yunbridge.main.mqtt\_certfile='/etc/ssl/certs/client.crt'  
uci set yunbridge.main.mqtt\_keyfile='/etc/ssl/private/client.key'  
uci commit yunbridge  
/etc/init.d/yunbridge restart

## **MQTT Usage Examples**

### **Pin Control Topics**

The primary topics use a standard schema for setting, getting, and reporting pin state.

| Action | Topic Schema | Payload | Example |
| :---- | :---- | :---- | :---- |
| **Set State** | yun/pin/\<N\>/set | ON, OFF, 1, or 0 | mosquitto\_pub \-t yun/pin/13/set \-m ON |
| **Request State** | yun/pin/\<N\>/get | (Any, typically empty) | mosquitto\_pub \-t yun/pin/13/get \-m "" |
| **State Report** | yun/pin/\<N\>/state | ON, OFF, 1, or 0 | (Subscribe to this topic) |

### **Advanced Commands**

Use the yun/command topic for administrative or advanced functions:

| Command | Topic | Payload Example | Function |
| :---- | :---- | :---- | :---- |
| **Key/Value Storage** | yun/command | SET \<key\> \<value\> | Store data on the Yun. |
| **Shell Execution** | yun/command | RUN echo hello | Execute a shell command. |
| **Read File** | yun/command | READFILE \<path\> | Read file contents. |
| **Mailbox Message** | yun/mailbox/send | \<message text\> | Send an arbitrary message to the Arduino sketch. |

## **Development & Troubleshooting**

### **Repository Structure**

Bridge-v2/                \# Arduino sketch and C++ bridge library  
openwrt-yun-v2/           \# OpenWRT system integration (init, configs)  
YunBridge-v2/             \# Python client and daemon source code  
YunWebUI-v2/              \# Web UI (LuCI) files  
compile.sh                \# Main script to build all packages  
install.sh                \# Main script to install pre-built artifacts on the Yun

### **Logs and Status Files**

Check these locations for debugging information. The LuCI Web UI provides a viewer for these logs.

* /tmp/yunbridge\_daemon.log (Main daemon operations)  
* /tmp/yunbridge\_mqtt\_plugin.log (MQTT connection issues)  
* /tmp/yunbridge\_debug.log (Detailed debug log, active when debug mode is enabled via UCI)  
* /tmp/yunbridge\_status.json (Real-time status file)

**Common Issues:**

* **Configuration changes not applied:** Always run uci commit yunbridge followed by /etc/init.d/yunbridge restart.  
* **Serial not found:** Verify /dev/ttyATH0 exists and is not conflicting with other processes (check /etc/inittab).  
* **Daemon not starting:** Check yunbridge\_daemon.log for **Config error:** messages or dependency issues.

### **Optimization: SD Card Storage**

To prevent RAM/flash from filling up due to temporary files and large logs, the installer automatically configures a bind mount to move the /tmp directory to the SD card (/mnt/sda1) if present.

**Verify status:**

df \-h /tmp

### **Using an External MQTT Broker**

It is recommended to use an external MQTT broker (e.g., on a PC, server, or cloud service) rather than installing one on the low-resource Yun.

To configure the external broker, simply set the host IP via UCI or the Web UI:  
uci set yunbridge.@bridge\[0\].mqtt\_host='\<external-broker-ip\>'

## **License**

This project is licensed under the **MIT License**.