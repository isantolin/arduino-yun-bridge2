# Arduino Yun v2 Ecosystem

This repository contains modernized, interoperable packages for Arduino Yun, compatible with legacy Bridge libraries and examples, and extended for new OpenWRT versions.

## Packages
- **Bridge-v2**: Arduino library (C++) for Yun, compatible with legacy Bridge API, with new features and bugfixes.
- **YunBridge-v2**: Python3-based bridge daemon for OpenWRT, compatible with legacy protocol, modular and extensible.
- **YunWebUI-v2**: Modern web interface, integrated with OpenWRT web server, REST/WebSocket APIs.
- **openwrt-yun-v2**: OpenWRT integration scripts, patches, and package definitions for seamless operation.


## Dependencies
## Dependencies
- Python 3 must be installed on OpenWRT:
	```sh
	opkg update
	opkg install python3
	```
+- The pyserial module is required:
	```sh
	opkg install python3-pyserial
	```

## Recommended Example Sketch

**For integration with YunBridge Python and the WebUI, use the generic sketch:**

`Bridge-v2/LED13BridgeControl.ino`

This sketch only turns LED 13 ON or OFF when it receives the correct command from OpenWRT, making it ideal for web and bridge testing.

## Installation Sequence
1. Flash your Yun with a modern OpenWRT image.
2. Install **openwrt-yun-v2** (see `/openwrt-yun-v2/install.sh`).
3. Install **YunBridge-v2** (see `/YunBridge-v2/install.sh`).
4. Install **YunWebUI-v2** (see `/YunWebUI-v2/install.sh`).
5. Install **Bridge-v2** Arduino library (see `/Bridge-v2/install.sh`).
6. Upload example sketches to your Yun and verify operation (LED 13 test included).

## Hardware Test
- All examples include a test for LED 13.
- See each package's README for step-by-step hardware test instructions.

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

## Roadmap
See `ROADMAP.md` for planned improvements and features.

---

# Hardware Test Instructions

## Prerequisites
- Arduino Yun with OpenWRT and all v2 packages installed
- Arduino IDE, SSH, and web browser

## Step-by-Step Tests
1. **LED 13 Test**
	- Upload `Bridge-v2/examples/LED13Test.ino` to your Yun.
	- Run `YunBridge-v2/examples/led13_test.py` on the Yun (SSH):
	  ```bash
	  python3 /path/to/YunBridge-v2/examples/led13_test.py
	  ```
	- Open YunWebUI in your browser and use LED 13 ON/OFF buttons.
	- LED 13 should respond in all cases.

2. **Full API Coverage**
	- Run or upload all example scripts/sketches in `Bridge-v2/examples` and `YunBridge-v2/examples`.
	- Verify correct responses and hardware behavior.

## Troubleshooting
- Ensure `/dev/ttyATH0` is present and not used by other processes.
- Check YunBridge daemon and CGI scripts are running.

---
