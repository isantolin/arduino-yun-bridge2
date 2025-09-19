# YunBridge v2

Modern Python3-based bridge daemon for Arduino Yun, compatible with legacy Bridge library and extended for new OpenWRT versions.

## Features
- Serial bridge on /dev/ttyATH0 @ 115200 baud
- Compatible with legacy Bridge protocol
- Modular, extensible, Python3 codebase
- Systemd service for auto-start
- REST/WebSocket API for YunWebUI integration


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

## Installation
See `install.sh` for step-by-step instructions.

## Hardware Test
- All examples include LED 13 blink/test
- See `examples/` for full API coverage

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---



# Recommended Arduino Sketch

**For integration with YunBridge Python and the WebUI, use:**

`Bridge-v2/LED13BridgeControl.ino`

This sketch only turns LED 13 ON or OFF when it receives the correct command from OpenWRT, making it ideal for web and bridge testing.

For advanced feature testing, you can also use:
- `Bridge-v2/examples/KVStoreTest.ino`
- `Bridge-v2/examples/ProcessTest.ino`
- `Bridge-v2/examples/FileIOTest.ino`
- `Bridge-v2/examples/MailboxTest.ino`
- `Bridge-v2/examples/ConsoleTest.ino`

# Hardware Test Instructions

## Prerequisites
- Arduino Yun with OpenWRT and Bridge-v2, YunBridge-v2 installed
- Connect via SSH to Yun for Python tests
- Arduino IDE for uploading sketches

## Step-by-Step Tests
1. **LED 13 Test**
	- Upload `Bridge-v2/examples/LED13Test.ino` to your Yun.
	- Run `YunBridge-v2/examples/led13_test.py` on the Yun (SSH):
	  ```bash
	  python3 /path/to/YunBridge-v2/examples/led13_test.py
	  ```
	- LED 13 should blink ON/OFF.

2. **Key-Value Store**
	- Upload `Bridge-v2/examples/KVStoreTest.ino` or run `YunBridge-v2/examples/kv_store_test.py`.
	- Check serial monitor or script output for correct SET/GET responses.

3. **Process Execution**
	- Upload `Bridge-v2/examples/ProcessTest.ino` or run `YunBridge-v2/examples/process_test.py`.
	- Output should show the result of the Linux command.

4. **File I/O**
	- Upload `Bridge-v2/examples/FileIOTest.ino` or run `YunBridge-v2/examples/fileio_test.py`.
	- Output should confirm file write/read.

5. **Mailbox**
	- Upload `Bridge-v2/examples/MailboxTest.ino` or run `YunBridge-v2/examples/mailbox_test.py`.
	- Output should show mailbox send/receive.

6. **Console**
	- Upload `Bridge-v2/examples/ConsoleTest.ino` or run `YunBridge-v2/examples/console_test.py`.
	- Output should show console message in YunBridge log.

## Troubleshooting
- Ensure `/dev/ttyATH0` is present and not used by other processes.
- Check baud rate is 115200.
- Use `dmesg` and `logread` for system logs.

---
