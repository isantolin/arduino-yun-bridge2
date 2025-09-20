
# Bridge v2

Arduino library for Yun, with exclusive support for MQTT. Support for legacy examples and sketches (classic Bridge) has been removed to advance the MQTT roadmap.

## Features
- MQTT support for IoT integration
- Main example: control of LED 13 via MQTT

## Installation
See `install.sh` for Arduino library installation steps.

## Hardware Test
- The main example is MQTT control of LED 13.

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)
- [YunBridge Library](https://docs.arduino.cc/retired/archived-libraries/YunBridgeLibrary/)

---

# Hardware Tests

## Requirements
- Arduino Yun with OpenWRT and all v2 packages installed
- Arduino IDE to upload the sketch

## Main Test
1. **LED 13 MQTT**
    - Upload `Bridge-v2/LED13BridgeControl.ino` to your Yun.
    - Run the MQTT test from the Yun or the WebUI.
    - LED 13 should respond in all cases.

## Troubleshooting
- Ensure the YunBridge daemon and the MQTT broker are running.

---

---
