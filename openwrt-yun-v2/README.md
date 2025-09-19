# openwrt-yun-v2

OpenWRT integration package for Bridge v2, YunBridge v2, and YunWebUI v2.

## Features
- Patches and scripts for modern OpenWRT
- Ensures /dev/ttyATH0 @ 115200 baud
- Systemd/init scripts for YunBridge
- Web UI integration


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
See `install.sh` for OpenWRT package installation and patching steps.

## Hardware Test
- Includes instructions for verifying serial bridge and web UI

## Documentation
- [Official Arduino Yun Guide](https://docs.arduino.cc/retired/getting-started-guides/ArduinoYun/)

---
# Hardware Test Instructions

- Arduino Yun with OpenWRT and all v2 packages installed

## Step-by-Step Tests
1. **Serial Bridge Test**
	- Run `echo 'LED13 ON' > /dev/ttyATH0` and verify LED 13 turns ON.
	- Run `echo 'LED13 OFF' > /dev/ttyATH0` and verify LED 13 turns OFF.

2. **CGI REST Test**
	- Access `http://yun.local/cgi-bin/led13?state=ON` in your browser.
	- LED 13 should turn ON and response should confirm.

## Troubleshooting
- Ensure `/dev/ttyATH0` is present and not used by other processes.
- Check permissions and executable bit on CGI scripts.


The install script will automatically:
- Copy the YunBridge daemon, init script, and config files
- Install the LED 13 CGI script as `/www/cgi-bin/led13` and set executable permissions
- Enable and start the bridge-v2 service

Manual steps (if needed):
```sh
chmod +x /etc/init.d/bridge-v2 /www/cgi-bin/led13
/etc/init.d/bridge-v2 enable
/etc/init.d/bridge-v2 start
```
---
