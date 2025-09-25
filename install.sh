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
opkg install python3 python3-pip || true
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
# Ensure swap is enabled on boot
if ! grep -q "$SWAPFILE" /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

# --- Install prebuilt packages ---
echo "[CHECKPOINT] Installing prebuilt .ipk packages..."
echo "[CHECKPOINT] Installing openwrt-yun-core .ipk first..."
for ipk in $CORE_IPK; do
    if [ -f "$ipk" ]; then
        echo "[INFO] Installing $ipk ..."
        opkg install --force-reinstall "$ipk"
    fi
done

echo "[CHECKPOINT] Installing remaining .ipk packages..."
for ipk in bin/*.ipk; do
    case "$ipk" in
        bin/openwrt-yun-core_*.ipk) continue;;
    esac
    if [ -f "$ipk" ]; then
        echo "[INFO] Installing $ipk ..."
        opkg install --force-reinstall "$ipk"
    fi
done


echo "[INFO] Prebuilt package installation complete."
echo "[CHECKPOINT] Running system conditioning steps (swap, daemon, Python packages)..."

## Python wheel installation (system Python)
# NOTE: Only prebuilt .whl files should be installed. Never attempt to build Python packages on OpenWRT.
echo "[openwrt-yun-core] Installing Python .whl packages using system Python..."
# Ensure a large enough temp dir for pip
export TMPDIR=/overlay/upper/tmp
mkdir -p "$TMPDIR"
for whl in bin/*.whl; do
    if [ -f "$whl" ]; then
        echo "[openwrt-yun-core] Installing $whl ..."
        if ! pip3 install --upgrade --force-reinstall "$whl"; then
            echo "[ERROR] Failed to install $whl" >&2
            exit 1
        fi
    fi
done
# --- Daemon Enable & Start ---
if [ -x /etc/init.d/yunbridge ]; then
    echo "[openwrt-yun-core] Enabling and starting yunbridge daemon ..."
    /etc/init.d/yunbridge enable
    /etc/init.d/yunbridge start
else
    echo "[WARNING] yunbridge init script not found at /etc/init.d/yunbridge." >&2
fi



echo "- Reboot the Yun if needed."
echo "- Test MQTT, LuCI WebUI, and integration."
echo "- For Amazon SNS support, ensure you have your AWS credentials and SNS topic ARN, and configure SNS options in LuCI."
