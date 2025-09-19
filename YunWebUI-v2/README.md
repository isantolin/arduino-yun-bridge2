# YunWebUI v2

Modern web interface for Arduino Yun, integrated with OpenWRT web server and compatible with Bridge v2 and YunBridge v2.

## Features
- HTML5/JS/CSS web UI
- REST and WebSocket integration with YunBridge
- Compatible with legacy and new features

## Installation
See `install.sh` for deployment steps to OpenWRT web server.

## Hardware Test
- Web UI includes LED 13 control and full API coverage

## REST Backend Integration
To enable LED 13 REST control:

The LED 13 CGI script is installed automatically by the openwrt-yun-v2 installer as `/www/cgi-bin/led13`.
If you need to reinstall manually:
```sh
cp openwrt-yun-v2/scripts/led13_rest_cgi.py /www/cgi-bin/led13
chmod +x /www/cgi-bin/led13
```

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)

---

# Hardware Test Instructions

## Prerequisites
- Arduino Yun with OpenWRT, YunWebUI-v2, YunBridge-v2, and Bridge-v2 installed
- Web browser on same network

## Step-by-Step Tests
1. **LED 13 Web Control**
	- Open the YunWebUI in your browser (e.g., `http://yun.local/arduino-webui-v2/`).
	- Use the ON/OFF buttons for LED 13.
	- LED 13 should turn ON/OFF and status should update.

## Troubleshooting
- Ensure `/cgi-bin/led13` CGI script is present and executable.
- Check YunBridge daemon is running.

---
