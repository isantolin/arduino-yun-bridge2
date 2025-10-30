#!/bin/sh
set -e
# This file is part of Arduino Yun Ecosystem v2.
# Copyright (C) 2025 Ignacio Santolin and contributors
# This program is free software: you can redistribute it and/or modify

set -e

echo "[STEP 1/6] Removing conflicting packages..."
opkg remove ppp ppp-mod-pppoe pppoe odhcp6c odhcpd --force-depends || true

#  --- Configuration Variables ---
SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=1024
INIT_SCRIPT="/etc/init.d/yunbridge"
export TMPDIR=/overlay/upper/tmp
#  --- Helper Functions ---
mkdir -p "$TMPDIR"
# Function to stop the yunbridge daemon robustly
stop_daemon() {
    if [ ! -x "$INIT_SCRIPT" ]; then
        echo "[INFO] YunBridge daemon not installed, skipping stop."
        return
    fi

    echo "[INFO] Stopping yunbridge daemon if active..."
    # First, try a graceful stop
    $INIT_SCRIPT stop 2>/dev/null || true
    sleep 1

    # Find any remaining yunbridge python processes
    pids=$(ps w | grep -E 'python[0-9.]*.*yunbridge' | grep -v grep | awk '{print $1}')

    if [ -n "$pids" ]; then
        echo "[WARN] Daemon still running. Sending SIGTERM..."
        kill $pids 2>/dev/null || true
        sleep 2 # Give it time to terminate

        # Final check and force kill
        pids2=$(ps w | grep -E 'python[0-9.]*.*yunbridge' | grep -v grep | awk '{print $1}')
        if [ -n "$pids2" ]; then
            echo "[WARN] Process will not die. Sending SIGKILL..."
            kill -9 $pids2 2>/dev/null || true
        fi
    else
        echo "[INFO] No running yunbridge daemon process found."
    fi
}
#  --- Main Script Execution ---

echo "[STEP 2/6] Updating system packages..."
opkg update
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade

echo "[STEP 3/6] Installing essential dependencies..."
#  Install essential packages one by one, checking first.
opkg install python3-pyserial-asyncio python3-aio-mqtt python3-asyncio python3-aio-mqtt-mod \
    coreutils-stty mosquitto-client-ssl luci

# ANÁLISIS: Se eliminó el bucle 'for pkg in $PACKAGES'
# Era código muerto: $PACKAGES no estaba definido y los paquetes
# ya se instalaron en el comando 'opkg install' anterior.

#  --- Stop Existing Daemon ---
stop_daemon
# --- Install Prebuilt Packages ---
echo "[STEP 4/6] Installing .ipk packages..."
#  Install all .ipk packages from the bin/ directory
if ls bin/*.ipk 1>/dev/null 2>&1; then
    opkg install --force-reinstall bin/*.ipk
fi

# --- System & LuCI Configuration ---
echo "[STEP 5/6] Finalizing system configuration..."
#  Remove serial console login to free up the port for the bridge
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
    echo "[INFO] Removing serial console login from /etc/inittab."
    sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi
#  Restart services to apply changes and load the new LuCI app
echo "[INFO] Restarting uhttpd and rpcd for LuCI..."
[ -f /etc/init.d/uhttpd ] && /etc/init.d/uhttpd restart
[ -f /etc/init.d/rpcd ] && /etc/init.d/rpcd restart
#  --- User Configuration & Daemon Start ---
echo "[STEP 6/6] Finalizing setup..."
#  Ask user if they want to enable debug mode by default
read -p "Do you want to enable YUNBRIDGE_DEBUG=1 by default for all users? [Y/n]: " yn
case $yn in
    [Nn])
        echo "[INFO] YUNBRIDGE_DEBUG will not be set by default."
        ;;
    *)
        mkdir -p /etc/profile.d
        echo "export YUNBRIDGE_DEBUG=1" > /etc/profile.d/yunbridge_debug.sh
        chmod +x /etc/profile.d/yunbridge_debug.sh
        echo "[INFO] YUNBRIDGE_DEBUG=1 will be set for all users on login."
        export YUNBRIDGE_DEBUG=1 # Export for current session
        ;;
esac
#  Enable and start the daemon
if [ -x "$INIT_SCRIPT" ]; then
    echo "[INFO] Enabling and starting yunbridge daemon..."
    $INIT_SCRIPT enable
    $INIT_SCRIPT restart
else
    echo "[WARNING] yunbridge init script not found at $INIT_SCRIPT." >&2
fi

echo -e "\n--- Installation Complete! ---"
echo "The YunBridge daemon is now running."
echo "You can configure it from the LuCI web interface under 'Services' > 'YunBridge'."
echo "A reboot is recommended if you encounter any issues."
