#!/bin/bash
# Simplified install script for Arduino Yun v2 ecosystem
# Installs only pre-built packages (.ipk, .whl) and performs minimal configuration

set -e

echo "[CHECKPOINT] Updating package lists..."
opkg update
echo "[CHECKPOINT] Upgrading upgradable packages..."
opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade
echo "[CHECKPOINT] Installing required opkg packages..."
# Only minimal system dependencies; all others are declared in each package's Makefile/setup.py
opkg install python3 python3-pip python3-venv || true
echo "[INFO] System Python packages installed."

# --- Instalar paquetes precompilados ---
echo "[CHECKPOINT] Instalando paquetes .ipk precompilados..."
echo "[CHECKPOINT] Installing openwrt-yun-core .ipk first..."
CORE_IPK="bin/openwrt-yun-core_*.ipk"
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



echo "[INFO] Instalación de paquetes precompilados completa."
echo "[CHECKPOINT] Running system conditioning steps (swap, daemon, Python packages)..."

# --- Swap Setup ---
SWAPFILE="/overlay/swapfile"
SWAPSIZE_MB=512
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
        echo "[ERROR] Failed to activate swap file." >&2
        exit 1
    fi
fi
# Ensure swap is enabled on boot
if ! grep -q "$SWAPFILE" /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
fi

## Python wheel installation (system Python)
echo "[openwrt-yun-core] Installing Python .whl packages using system Python..."
for whl in ./*.whl; do
    if [ -f "$whl" ]; then
        echo "[openwrt-yun-core] Installing $whl ..."
        if ! pip3 install --upgrade "$whl"; then
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
echo "- For Google Pub/Sub support, ensure you have a valid service account .json file and configure Pub/Sub options in LuCI."
echo "- For Amazon SNS support, ensure you have your AWS credentials and SNS topic ARN, and configure SNS options in LuCI."
