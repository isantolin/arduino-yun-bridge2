

# Bridge v2

Arduino library for Yun, with exclusive support for MQTT. Support for legacy examples and sketches (classic Bridge) has been removed to advance the MQTT roadmap.

## Features
- MQTT support for IoT integration
- Main example: generic pin control via MQTT (default: pin 13, but any pin can be used)

## Installation
See `install.sh` for Arduino library installation steps.

## Hardware Test
- The main example is MQTT control of any pin (default: 13).

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---

# Hardware Tests

## Requirements
- Arduino Yun with OpenWRT and all v2 packages installed
- Arduino IDE to upload the sketch

## Main Test
1. **Generic Pin MQTT**
    - Upload `Bridge-v2/LED13BridgeControl.ino` to your Yun.
    - Run the MQTT test from the Yun or the WebUI, specifying the pin number if desired (default is 13).
    - The selected pin should respond in all cases.

## Troubleshooting
- Ensure the YunBridge daemon and the MQTT broker are running.

---
