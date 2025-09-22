#!/bin/bash
# Unified install script for Arduino Yun v2 ecosystem
# Installs all dependencies, daemon, scripts, configs, Arduino library, and Python client plugin system

set -e

# 1. Update and upgrade system
opkg update
 # Upgrade only packages with new versions available
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade


echo "[INFO] Installing/updating paho-mqtt, google-cloud-pubsub, and boto3 for Python3..."
python3 -m pip install --upgrade paho-mqtt google-cloud-pubsub boto3

opkg install python3-uci python3 python3-pyserial mosquitto python3-pip || true

echo "[INFO] Core installation complete."
echo "[INFO] To install the Web UI (LuCI), follow instructions in /luci-app-yunbridge/README.md."

# 3. Remove serial console login if present
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
    echo "[INFO] Removing serial console login from /etc/inittab..."
    sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi

# 4. Install CGI REST script

# 1. Update and upgrade system
opkg update
 # Upgrade only packages with new versions available
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade

# (System update and core dependencies already handled above)
if [ -f openwrt-yun-core/scripts/pin_rest_cgi.py ]; then
    mkdir -p /www/cgi-bin
    cp -f openwrt-yun-core/scripts/pin_rest_cgi.py /www/cgi-bin/pin
    chmod +x /www/cgi-bin/pin
    echo "Installing REST CGI script (generic pin, requires pin parameter)..."
else
    echo "WARNING: openwrt-yun-core/scripts/pin_rest_cgi.py not found. CGI script not installed."
fi

# 5. Ensure /etc/yunbridge exists
if [ ! -d /etc/yunbridge ]; then
    mkdir -p /etc/yunbridge || { echo "ERROR: Could not create /etc/yunbridge"; exit 1; }
fi

# 3. Install LuCI Web UI if present
LUCI_IPK=$(ls luci-app-yunbridge/bin/packages/*/luci/luci-app-yunbridge_*.ipk 2>/dev/null | head -n1)
if [ -n "$LUCI_IPK" ]; then
    echo "[INFO] Installing Web UI (luci-app-yunbridge) from .ipk package..."
    opkg install "$LUCI_IPK"
    echo "[INFO] Web UI (LuCI) installed from .ipk. Access via LuCI > Services > YunBridge."
else
    if [ -d luci-app-yunbridge/luasrc ]; then
    echo "[INFO] Installing Web UI (luci-app-yunbridge) manually..."
        mkdir -p /usr/lib/lua/luci/controller
        mkdir -p /usr/lib/lua/luci/model/cbi
        mkdir -p /usr/lib/lua/luci/view
        cp -f luci-app-yunbridge/luasrc/controller/yunbridge.lua /usr/lib/lua/luci/controller/ 2>/dev/null || true
        cp -f luci-app-yunbridge/luasrc/model/cbi/yunbridge.lua /usr/lib/lua/luci/model/cbi/ 2>/dev/null || true
        cp -rf luci-app-yunbridge/luasrc/view/yunbridge /usr/lib/lua/luci/view/ 2>/dev/null || true
        # Config UCI
        if [ -f luci-app-yunbridge/root/etc/config/yunbridge ]; then
            cp -f luci-app-yunbridge/root/etc/config/yunbridge /etc/config/yunbridge
        fi
        # WebUI
        mkdir -p /www/yunbridge
        if [ -f luci-app-yunbridge/root/www/yunbridge/index.html ]; then
            cp -f luci-app-yunbridge/root/www/yunbridge/index.html /www/yunbridge/index.html
        fi
        # Restart LuCI services
        if [ -f /etc/init.d/uhttpd ]; then
            /etc/init.d/uhttpd restart
        fi
        if [ -f /etc/init.d/rpcd ]; then
            /etc/init.d/rpcd restart
        fi
    echo "[INFO] Web UI (LuCI) installed manually. Access via LuCI > Services > YunBridge."
    else
    echo "[INFO] Web UI (luci-app-yunbridge) not found, only core installed."
    fi
fi

# 6. Copy config and package files
# Map and install config/package files to correct locations
if [ -f openwrt-yun-core/package/99-yunbridge-ttyath0.conf ]; then
    cp -f openwrt-yun-core/package/99-yunbridge-ttyath0.conf /etc/config/yunbridge-ttyath0
else
    echo "WARNING: openwrt-yun-core/package/99-yunbridge-ttyath0.conf not found."
fi
if [ -f openwrt-yun-core/package/yunbridge.files ]; then
    cp -f openwrt-yun-core/package/yunbridge.files /etc/yunbridge/yunbridge.files
else
    echo "WARNING: openwrt-yun-core/package/yunbridge.files not found."
fi

# 7. Install init script
if [ -f openwrt-yun-core/package/yunbridge.init ]; then
    cp -f openwrt-yun-core/package/yunbridge.init /etc/init.d/yunbridge
    chmod +x /etc/init.d/yunbridge
else
    echo "ERROR: openwrt-yun-core/package/yunbridge.init not found."
fi

# 8. Copy scripts to /usr/bin
if [ -d openwrt-yun-core/scripts ]; then
    for f in openwrt-yun-core/scripts/*; do
        if [ -f "$f" ]; then
            cp -f "$f" /usr/bin/
        fi
    done
else
    echo "WARNING: openwrt-yun-core/scripts directory not found."
fi

# 9. Install YunBridge daemon (Python package)
if [ -f openwrt-yun-bridge/setup.py ]; then
    echo "[INFO] Installing Python daemon openwrt-yun-bridge via setup.py..."
    cd openwrt-yun-bridge
    python3 -m pip install --force-reinstall --upgrade .
    cd ..
    echo "[INFO] Daemon yunbridge installed as Python package. Run 'yunbridge' to launch."
else
    echo "ERROR: openwrt-yun-bridge/setup.py not found."
fi

# 10. Stop any running yunbridge daemons before starting a new one
PIDS=$(ps | grep '[y]unbridge' | awk '{print $1}')
if [ -n "$PIDS" ]; then
    echo "Stopping YunBridge v2 daemon..."
    kill $PIDS
fi

# 11. Start YunBridge daemon
if command -v python3 >/dev/null 2>&1; then
    echo "[DEBUG] Launching YunBridge daemon in background and showing real-time log..."
    python3 /usr/bin/yunbridge > /tmp/yunbridge_debug.log 2>&1 &
    sleep 1
    tail -f /tmp/yunbridge_debug.log &
    echo "YunBridge daemon started. Log is shown above. You can close the tail with Ctrl+C."
else
    echo "ERROR: python3 not found. Daemon not started."
fi

# 12. Install Arduino library (openwrt-library-arduino)
if [ -d openwrt-library-arduino/src ]; then
    LIB_DST="$HOME/Arduino/libraries/openwrt-library-arduino"
    mkdir -p "$LIB_DST"
    cp -rf openwrt-library-arduino/src/* "$LIB_DST/"
    echo "openwrt-library-arduino installed to $LIB_DST."
else
    echo "WARNING: openwrt-library-arduino/src directory not found. Arduino library not installed."
fi

echo "\n[INFO] Arduino Yun v2 ecosystem installation complete."
echo "- Upload the example sketch from openwrt-library-arduino to your Yun using the Arduino IDE."
echo "- Reboot the Yun if needed."
echo "- Test MQTT, LuCI WebUI, and integration."
echo "- For Google Pub/Sub support, ensure you have a valid service account .json file and configure Pub/Sub options in LuCI."
echo "- For Amazon SNS support, ensure you have your AWS credentials and SNS topic ARN, and configure SNS options in LuCI."
