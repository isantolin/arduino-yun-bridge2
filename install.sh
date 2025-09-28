#!/bin/sh
#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#!/bin/bash
# Refactored install script for Arduino Yun v2 ecosystem
# Uses functions to remove redundancy and improves process handling.

set -e

# --- Configuration Variables ---
SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=1024
INIT_SCRIPT="/etc/init.d/yunbridge"
export TMPDIR=/overlay/upper/tmp

# --- Helper Functions ---
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

# Function to create and format the swap file
create_swap_file() {
    echo "[INFO] Creating swap file at $SWAPFILE (${SWAPSIZE_MB}MB)..."
    if ! dd if=/dev/zero of="$SWAPFILE" bs=1M count=$SWAPSIZE_MB; then
        echo "[ERROR] Failed to create swap file block." >&2
        return 1
    fi
    if ! mkswap "$SWAPFILE"; then
        echo "[ERROR] Failed to format swap file." >&2
        return 1
    fi
    chmod 600 "$SWAPFILE"
    return 0
}


# --- Main Script Execution ---

echo "[STEP 1/6] Updating system packages..."
opkg update
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade

echo "[STEP 2/6] Installing essential dependencies..."
# Minimal system dependencies. || true prevents script exit on non-fatal errors.
opkg install python3 python3-pip luci-compat luci-mod-admin-full lua luci-lib-nixio luci-lib-json python3-pyserial python3-paho-mqtt || true

# --- Stop Existing Daemon ---
stop_daemon

# --- Swap File Management ---
echo "[STEP 3/6] Setting up swap file..."
if [ ! -f "$SWAPFILE" ]; then
    create_swap_file || exit 1
fi

# Check if swap is on, if not, try to activate it.
if ! swapon -s | grep -q "$SWAPFILE"; then
    echo "[INFO] Activating swap file..."
    if ! swapon "$SWAPFILE"; then
        echo "[WARN] Failed to activate swap. Attempting to recreate..."
        swapoff "$SWAPFILE" 2>/dev/null || true
        rm -f "$SWAPFILE"
        create_swap_file || exit 1
        if ! swapon "$SWAPFILE"; then
            echo "[ERROR] Failed to activate swap file after recreation." >&2
            exit 1
        fi
    fi
fi

# Ensure swap is enabled on boot via /etc/fstab
if ! grep -q "$SWAPFILE" /etc/fstab; then
    echo "[INFO] Adding swap file to /etc/fstab for boot-time activation."
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

# --- Install Prebuilt Packages ---
echo "[STEP 4/6] Installing .ipk and .whl packages..."

# Install all .ipk packages from the bin/ directory
if ls bin/*.ipk 1>/dev/null 2>&1; then
    opkg install --force-reinstall bin/*.ipk
fi


# Instalar solo los .whl de openwrt_yun_client_python si existen
#for whl in bin/openwrt_yun_client_python-*.whl; do
#    if [ -e "$whl" ]; then
#        echo "[INFO] Installing Python package: $whl"
#        if ! pip3 install --upgrade --force-reinstall --no-deps "$whl"; then
#            echo "[ERROR] Failed to install $whl" >&2
#            exit 1
#        fi
#    fi
#done

# --- System & LuCI Configuration ---
echo "[STEP 5/6] Finalizing system configuration..."

# Remove serial console login to free up the port for the bridge
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
    echo "[INFO] Removing serial console login from /etc/inittab."
    sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi

# Restart services to apply changes and load the new LuCI app
echo "[INFO] Restarting uhttpd and rpcd for LuCI..."
[ -f /etc/init.d/uhttpd ] && /etc/init.d/uhttpd restart
[ -f /etc/init.d/rpcd ] && /etc/init.d/rpcd restart

# --- User Configuration & Daemon Start ---
echo "[STEP 6/6] Finalizing setup..."

# Ask user if they want to enable debug mode by default
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

# Enable and start the daemon
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