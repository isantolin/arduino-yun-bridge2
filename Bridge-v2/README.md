# Bridge v2

Modern Arduino library for Yun, compatible with legacy Bridge API and extended for new features.

## Features
- Compatible with legacy Bridge sketches
- New features and bugfixes
- Examples include LED 13 test and full API coverage

## Installation
See `install.sh` for Arduino library installation steps.

## Hardware Test
- All examples include LED 13 blink/test

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---

# Hardware Test Instructions

## Prerequisites
- Arduino Yun with OpenWRT and Bridge-v2, YunBridge-v2 installed
- Arduino IDE for uploading sketches

## Step-by-Step Tests
1. **LED 13 Test**
	- Upload `examples/LED13Test.ino` to your Yun.
	- LED 13 should blink ON/OFF and YunBridge should log commands.

2. **Key-Value Store**
	- Upload `examples/KVStoreTest.ino`.
	- Open Serial Monitor at 9600 baud to see SET/GET responses.

3. **Process Execution**
	- Upload `examples/ProcessTest.ino`.
	- Open Serial Monitor to see Linux command output.

4. **File I/O**
	- Upload `examples/FileIOTest.ino`.
	- Open Serial Monitor to see file write/read confirmation.

5. **Mailbox**
	- Upload `examples/MailboxTest.ino`.
	- Open Serial Monitor to see mailbox send/receive.

6. **Console**
	- Upload `examples/ConsoleTest.ino`.
	- YunBridge log should show console message.

## Troubleshooting
- Ensure YunBridge daemon is running on Yun.
- Check Serial1 is set to 115200 baud.

---
