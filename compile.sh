#!/bin/bash
# Compile script for Arduino Yun v2 ecosystem
# Builds all .ipk and .whl packages on the PC. Never build wheels on the device.

set -e

# --- OpenWRT SDK Build ---
echo "[CHECKPOINT] Building OpenWRT .ipk packages..."
cd openwrt-yun-v2
./install.sh
cd ..

echo "[CHECKPOINT] Copying .ipk files to bin/ ..."
mkdir -p bin
cp openwrt-yun-v2/bin/*.ipk bin/ || true

# --- Python Wheel Build ---
echo "[CHECKPOINT] Building Python .whl packages on PC..."
for d in YunBridge-v2 openwrt-yun-client-python; do
    if [ -d "$d" ]; then
        cd "$d"
        if [ -f setup.py ]; then
            echo "[INFO] Building wheel for $d ..."
            python3 setup.py bdist_wheel
            cp dist/*.whl ../bin/
        fi
        cd ..
    fi
done

echo "[CHECKPOINT] Cleaning up build artifacts..."
for d in YunBridge-v2 openwrt-yun-client-python; do
    if [ -d "$d" ]; then
        rm -rf "$d"/build "$d"/dist "$d"/*.egg-info
    fi
done

echo "[INFO] All .ipk and .whl packages built and copied to bin/."
