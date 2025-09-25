#!/bin/bash
# Simplified install script for Arduino Yun v2 ecosystem
# Installs only pre-built packages (.ipk, .whl) and performs minimal configuration

set -e

SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=1024
CORE_IPK="bin/openwrt-yun-core_*.ipk"

echo "[CHECKPOINT] Updating package lists..."
opkg update
echo "[CHECKPOINT] Upgrading upgradable packages..."
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade
echo "[CHECKPOINT] Installing required opkg packages..."
# Only minimal system dependencies; all others are declared in each package's Makefile/setup.py
opkg install python3 python3-pip luci-compat luci-mod-admin-full lua luci-lib-nixio luci-lib-json|| true
echo "[INFO] System Python packages installed."
# --- Swap Setup ---

# Swap logic: only create if not present, never delete if exists
if [ ! -f "$SWAPFILE" ]; then
    echo "[openwrt-yun-core] Creating swap file at $SWAPFILE (${SWAPSIZE_MB}MB) ..."
    if ! dd if=/dev/zero of="$SWAPFILE" bs=1M count=$SWAPSIZE_MB; then
        echo "[ERROR] Failed to create swap file." >&2
        exit 1
    fi
    if ! mkswap "$SWAPFILE"; then
        echo "[ERROR] Failed to format swap file." >&2
        exit 1
    fi
    chmod 600 "$SWAPFILE"
fi
if ! swapon -s | grep -q "$SWAPFILE"; then
    echo "[openwrt-yun-core] Activating swap file $SWAPFILE ..."
    if ! swapon "$SWAPFILE"; then
        echo "[WARN] Failed to activate swap file. Attempting to recreate..."
        swapoff "$SWAPFILE" 2>/dev/null || true
        rm -f "$SWAPFILE"
        echo "[openwrt-yun-core] Recreating swap file at $SWAPFILE (${SWAPSIZE_MB}MB) ..."
        if ! dd if=/dev/zero of="$SWAPFILE" bs=1M count=$SWAPSIZE_MB; then
            echo "[ERROR] Failed to create swap file." >&2
            exit 1
        fi
        if ! mkswap "$SWAPFILE"; then
            echo "[ERROR] Failed to format swap file." >&2
            exit 1
        fi
        chmod 600 "$SWAPFILE"
        if ! swapon "$SWAPFILE"; then
            echo "[ERROR] Failed to activate swap file after recreation." >&2
            exit 1
        fi
    fi
fi

# Ensure swap is enabled on boot via /etc/fstab
if ! grep -q "$SWAPFILE" /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

# Also ensure swap is activated in /etc/rc.local (workaround for OpenWRT boot order issues)
if [ -f /etc/rc.local ]; then
    if ! grep -q "swapon $SWAPFILE" /etc/rc.local; then
        # Insert before final 'exit 0' if present, else append
        if grep -q '^exit 0' /etc/rc.local; then
            sed -i "/^exit 0/i swapon $SWAPFILE" /etc/rc.local
        else
            echo "swapon $SWAPFILE" >> /etc/rc.local
        fi
        echo "[openwrt-yun-core] Added swapon $SWAPFILE to /etc/rc.local for boot-time activation."
    fi
fi

# --- Install prebuilt packages ---
echo "[CHECKPOINT] Installing openwrt-yun-core .ipk first..."
echo "[CHECKPOINT] Installing remaining .ipk packages..."

echo "[CHECKPOINT] Installing all .ipk packages in bin/ ..."
if ls bin/*.ipk 1>/dev/null 2>&1; then
    opkg install --force-reinstall bin/*.ipk
fi



echo "[INFO] Prebuilt package installation complete."

# Restart uhttpd and rpcd to ensure LuCI app is available after install
if [ -f /etc/init.d/uhttpd ]; then
    /etc/init.d/uhttpd restart
fi
if [ -f /etc/init.d/rpcd ]; then
    /etc/init.d/rpcd restart
fi
# Check if LuCI controller is in the correct location
if [ ! -f /usr/lib/lua/luci/controller/yunbridge.lua ]; then
    echo "[WARNING] LuCI controller not found at /usr/lib/lua/luci/controller/yunbridge.lua."
    echo "[WARNING] The YunBridge menu will NOT appear in LuCI."
    echo "[HINT] Ensure your package installs luasrc/luci/controller/yunbridge.lua to this path."
else
    echo "[INFO] LuCI app for YunBridge installed. Access via LuCI > Services > YunBridge."
fi
echo "[CHECKPOINT] Running system conditioning steps (swap, daemon, Python packages, serial console cleanup)..."

# Remove serial console login if present (prevents login prompt on serial port)
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
    echo "[INFO] Removing serial console login from /etc/inittab..."
    sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi

## Python wheel installation (system Python)
# NOTE: Only prebuilt .whl files should be installed. Never attempt to build Python packages on OpenWRT.
echo "[openwrt-yun-core] Installing Python .whl packages using system Python..."
# Ensure a large enough temp dir for pip
export TMPDIR=/overlay/upper/tmp
mkdir -p "$TMPDIR"
# POSIX-compliant: install all .whl files in bin/ in a single pip3 command (if any exist)
set -- bin/*.whl
if [ -e "$1" ]; then
    echo "[openwrt-yun-core] Installing all .whl packages in bin/ ..."
    if ! pip3 install --upgrade --force-reinstall "$@"; then
        echo "[ERROR] Failed to install one or more .whl packages" >&2
        exit 1
    fi
fi

# --- Ask user if they want to enable debug mode by default ---
read -p "Do you want to enable YUNBRIDGE_DEBUG=1 by default for all users? [y/N]: " yn
case $yn in
    [Yy]*)
        echo "export YUNBRIDGE_DEBUG=1" > /etc/profile.d/yunbridge_debug.sh
        chmod +x /etc/profile.d/yunbridge_debug.sh
        echo "[INFO] YUNBRIDGE_DEBUG=1 will be set for all users on login."
        ;;
    *)
        echo "[INFO] YUNBRIDGE_DEBUG will not be set by default."
        ;;
esac

# --- Daemon Enable & Start ---
if [ -x /etc/init.d/yunbridge ]; then
    echo "[openwrt-yun-core] Enabling and starting yunbridge daemon ..."
    /etc/init.d/yunbridge enable
    /etc/init.d/yunbridge restart
else
    echo "[WARNING] yunbridge init script not found at /etc/init.d/yunbridge." >&2
fi



echo "- Reboot the Yun if needed."
echo "- Test MQTT, LuCI WebUI, and integration."
echo "- For Amazon SNS support, ensure you have your AWS credentials and SNS topic ARN, and configure SNS options in LuCI."
