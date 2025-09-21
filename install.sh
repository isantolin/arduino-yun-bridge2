#!/bin/bash
# Unified install script for Arduino Yun v2 ecosystem
# Installs all dependencies, daemon, scripts, configs, and Arduino library

set -e

# 1. Update and upgrade system
opkg update
# Actualizar solo los paquetes que tengan nueva versión disponible
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade

# 2. Install dependencies and WebUI and LuCI dependencies
opkg install luci luci-base lua luci-mod-admin-full luci-lib-nixio luci-lib-ipkg luci-compat python3-uci python3 python3-pyserial mosquitto python3-pip || true

echo "[INFO] Instalando/actualizando paho-mqtt para Python3..."
python3 -m pip install --upgrade paho-mqtt
# 2b. Install/Copy LuCI app files (if present)
if [ -d openwrt-yun-v2/luci-app-yunbridge/luasrc ]; then
    echo "[INFO] Installing LuCI app files for YunBridge..."
    mkdir -p /usr/lib/lua/luci/controller
    mkdir -p /usr/lib/lua/luci/model/cbi
    mkdir -p /usr/lib/lua/luci/view
    cp -f openwrt-yun-v2/luci-app-yunbridge/luasrc/controller/yunbridge.lua /usr/lib/lua/luci/controller/ 2>/dev/null || true
    cp -f openwrt-yun-v2/luci-app-yunbridge/luasrc/model/cbi/yunbridge.lua /usr/lib/lua/luci/model/cbi/ 2>/dev/null || true
    cp -rf openwrt-yun-v2/luci-app-yunbridge/luasrc/view/yunbridge /usr/lib/lua/luci/view/ 2>/dev/null || true
    # Config UCI
    if [ -f openwrt-yun-v2/luci-app-yunbridge/root/etc/config/yunbridge ]; then
        cp -f openwrt-yun-v2/luci-app-yunbridge/root/etc/config/yunbridge /etc/config/yunbridge
    fi
    # WebUI
    mkdir -p /www/yunbridge
    if [ -f openwrt-yun-v2/luci-app-yunbridge/root/www/yunbridge/index.html ]; then
        cp -f openwrt-yun-v2/luci-app-yunbridge/root/www/yunbridge/index.html /www/yunbridge/index.html
    fi
    # Restart LuCI services
    if [ -f /etc/init.d/uhttpd ]; then
        /etc/init.d/uhttpd restart
    fi
    if [ -f /etc/init.d/rpcd ]; then
        /etc/init.d/rpcd restart
    fi
    echo "[INFO] LuCI app for YunBridge installed. Access via LuCI > Servicios > YunBridge."
else
    echo "[INFO] LuCI app source not found, skipping LuCI app install."
fi

# 3. Remove serial console login if present
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
    echo "[INFO] Removing serial console login from /etc/inittab..."
    sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi

# 4. Install CGI REST script
if [ -f openwrt-yun-v2/scripts/pin_rest_cgi.py ]; then
    mkdir -p /www/cgi-bin
    cp -f openwrt-yun-v2/scripts/pin_rest_cgi.py /www/cgi-bin/pin
    chmod +x /www/cgi-bin/pin
    echo "Installing REST CGI script (generic pin, requires pin parameter)..."
else
    echo "WARNING: openwrt-yun-v2/scripts/pin_rest_cgi.py not found. CGI script not installed."
fi

# 5. Ensure /etc/yunbridge exists
if [ ! -d /etc/yunbridge ]; then
    mkdir -p /etc/yunbridge || { echo "ERROR: Could not create /etc/yunbridge"; exit 1; }
fi

# 6. Copy config and package files
for f in openwrt-yun-v2/package/99-bridge-ttyath0.conf openwrt-yun-v2/package/README.md openwrt-yun-v2/package/bridge-v2.files; do
    if [ -f "$f" ]; then
    cp -f "$f" /etc/yunbridge/
    else
        echo "WARNING: $f not found."
    fi
done

# 7. Install init script
if [ -f openwrt-yun-v2/package/bridge-v2.init ]; then
    cp -f openwrt-yun-v2/package/bridge-v2.init /etc/init.d/bridge-v2
    chmod +x /etc/init.d/bridge-v2
else
    echo "ERROR: openwrt-yun-v2/package/bridge-v2.init not found."
fi

# 8. Copy scripts to /usr/bin
if [ -d openwrt-yun-v2/scripts ]; then
    for f in openwrt-yun-v2/scripts/*; do
        if [ -f "$f" ]; then
            cp -f "$f" /usr/bin/
        fi
    done
else
    echo "WARNING: openwrt-yun-v2/scripts directory not found."
fi

# 9. Install YunBridge daemon
if [ -f YunBridge-v2/src/bridge_daemon.py ]; then
    echo "Copying YunBridge daemon..."
    cp -f YunBridge-v2/src/bridge_daemon.py /usr/bin/yunbridge
    chmod +x /usr/bin/yunbridge
else
    echo "ERROR: YunBridge-v2/src/bridge_daemon.py not found."
fi

# 10. Stop any running yunbridge daemons before starting a new one
PIDS=$(ps | grep '[y]unbridge' | awk '{print $1}')
if [ -n "$PIDS" ]; then
    echo "Stopping YunBridge v2 daemon..."
    kill $PIDS
fi

# 11. Start YunBridge daemon
if command -v python3 >/dev/null 2>&1; then
    echo "[DEBUG] Lanzando YunBridge daemon en background y mostrando log en tiempo real..."
    python3 /usr/bin/yunbridge > /tmp/yunbridge_debug.log 2>&1 &
    sleep 1
    tail -f /tmp/yunbridge_debug.log &
    echo "YunBridge daemon started. El log se muestra arriba. Puedes cerrar el tail con Ctrl+C."
else
    echo "ERROR: python3 not found. Daemon not started."
fi

# 12. Install Arduino library (Bridge-v2)
if [ -d Bridge-v2/src ]; then
    LIB_DST="$HOME/Arduino/libraries/Bridge-v2"
    mkdir -p "$LIB_DST"
    cp -rf Bridge-v2/src/* "$LIB_DST/"
    echo "Bridge v2 library installed to $LIB_DST."
else
    echo "WARNING: Bridge-v2/src directory not found. Arduino library not installed."
fi

echo "\n[INFO] Arduino Yun v2 ecosystem installation complete."
echo "- Upload the example sketch from Bridge-v2 to your Yun using the Arduino IDE."
echo "- Reboot the Yun if needed."
echo "- Test MQTT, LuCI WebUI, and integration."
