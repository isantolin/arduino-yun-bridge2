# Dependencias necesarias

Antes de instalar el panel, asegúrate de tener instalados los siguientes paquetes en OpenWrt:


## Required Dependencies

Before installing the panel, make sure you have the following packages installed on OpenWrt:

```sh
opkg update
opkg install luci luci-base lua luci-mod-admin-full luci-lib-nixio luci-lib-ipkg luci-compat python3-uci
```

# luci-app-yunbridge

LuCI Web panel for configuring and monitoring YunBridge on OpenWrt.

## Features
- Edit YunBridge configuration (MQTT, serial, debug, etc.)
- Integrated Web UI for real-time control/monitoring (from YunWebUI)
- UCI config: `/etc/config/yunbridge`

## Configuration Notes
The YunBridge daemon reads configuration from UCI (`/etc/config/yunbridge`) using `python3-uci`. If an option does not exist, the default value will be used.

## Manual Installation (without buildroot)

You can install the panel directly by copying the files to the router via SSH/SCP:

1. Run the automatic installation script:
    ```sh
    ./install.sh <ROUTER_IP> [user]
    # Example:
    ./install.sh 192.168.1.1 root
    ```
    This will copy all necessary files and restart LuCI.

2. Alternatively, copy manually:
    - `luasrc/controller/yunbridge.lua` to `/usr/lib/lua/luci/controller/`
    - `luasrc/model/cbi/yunbridge.lua` to `/usr/lib/lua/luci/model/cbi/`
    - `luasrc/view/yunbridge/` to `/usr/lib/lua/luci/view/`
    - `root/etc/config/yunbridge` to `/etc/config/yunbridge`
    - `root/www/yunbridge/index.html` to `/www/yunbridge/index.html`
    - Restart LuCI: `/etc/init.d/uhttpd restart; /etc/init.d/rpcd restart`

Then access LuCI: Services > YunBridge

## Usage
- Install the package or copy the files manually
- Access via LuCI: Services > YunBridge
- Configure parameters and use the Web UI tab

## Files
- `luasrc/controller/yunbridge.lua`: LuCI controller
- `luasrc/model/cbi/yunbridge.lua`: Config form
- `luasrc/view/yunbridge/webui.htm`: Embedded Web UI
- `root/etc/config/yunbridge`: UCI config defaults
- `root/www/yunbridge/index.html`: YunWebUI frontend (copy from YunWebUI-v2)


## MQTT Broker IP Automation
// WebSocket support is not available in the default OpenWrt Mosquitto package. Use standard MQTT (port 1883) for now.

## Roadmap
- [ ] Improve parameter validation
- [ ] Support for more advanced options
- [ ] Integration with daemon logs and status
