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
# compile.sh - Compila todos los paquetes del ecosistema Arduino Yun v2
# Descarga y prepara el buildroot de OpenWRT si es necesario, compila los paquetes OpenWRT y Python, y deja los artefactos listos en bin/
#
# Uso: ./compile.sh
set -e

OPENWRT_VERSION="24.10.3"
OPENWRT_URL="https://downloads.openwrt.org/releases/"$OPENWRT_VERSION"/targets/ath79/generic/openwrt-sdk-"$OPENWRT_VERSION"-ath79-generic_gcc-13.3.0_musl.Linux-x86_64.tar.zst"
SDK_DIR="openwrt-sdk"
BIN_DIR="bin"


echo "[INFO] Installing build dependencies required for OpenWRT SDK (development PC only)"
if [ "$(uname -s)" = "Linux" ]; then
    if [ -f /etc/debian_version ]; then
        echo "[INFO] Installing packages for Ubuntu/Debian..."
        sudo apt-get update
        sudo apt-get install -y build-essential python3 python3-pip python3-setuptools python3-wheel python3-build git unzip tar gzip bzip2 xz-utils coreutils libncurses5-dev libncursesw5-dev zstd wget
    elif [ -f /etc/fedora-release ]; then
        echo "[INFO] Installing packages for Fedora..."
        # sudo dnf clean all
        # sudo dnf update
        sudo dnf install -y make automake gcc gcc-c++ kernel-devel python3 python3-pip python3-setuptools python3-wheel python3-build git unzip tar gzip bzip2 xz coreutils ncurses-devel zstd wget
    else
        echo "[WARN] Unrecognized Linux distro. Please install manually: build-essential, ncurses-dev, zstd, wget, etc."
    fi
else
    echo "[WARN] Operating system not supported for automatic dependency installation."
fi

echo "[INFO] Preparing build environment..."
mkdir -p "$BIN_DIR"

# 1. Download and extract the buildroot/SDK if it does not exist, with retry logic for data corruption
if [ ! -d "$SDK_DIR" ]; then
    MAX_RETRIES=5
    RETRY=0
    SUCCESS=0
    while [ $RETRY -lt $MAX_RETRIES ]; do
        RETRY_COUNT=$(expr $RETRY + 1)
        echo "[INFO] Downloading OpenWRT SDK (attempt $RETRY_COUNT/$MAX_RETRIES)..."
        wget -O sdk.tar.zst "$OPENWRT_URL"
        echo "[INFO] Extracting SDK..."
        if tar --use-compress-program=unzstd -xf sdk.tar.zst; then
            rm sdk.tar.zst
            mv openwrt-sdk-* "$SDK_DIR"
            SUCCESS=1
            break
        else
            echo "[WARN] SDK extraction failed (possible data corruption). Retrying..."
            rm -f sdk.tar.zst
            # Clean up any partial extraction
            rm -rf openwrt-sdk-*
            RETRY=$(expr $RETRY + 1)
            sleep 2
        fi
    done
    if [ $SUCCESS -ne 1 ]; then
        echo "[ERROR] Failed to download and extract OpenWRT SDK after $MAX_RETRIES attempts. Exiting."
        exit 1
    fi
fi

# 2. Copy OpenWRT packages to buildroot/SDK (after feeds are updated and luci-base is installed)

# Always copy latest package sources into SDK/package (prevents stale/missing package errors)


# Always copy latest package sources into SDK/package (prevents stale/missing package errors)

# Robust sync for openwrt-yun-bridge: ensure bridge_daemon.py and yunbridge.init are present
for pkg in luci-app-yunbridge openwrt-yun-core openwrt-yun-bridge; do
    if [ -d "$pkg" ]; then
        echo "[INFO] Syncing $pkg to SDK..."
        rm -rf "$SDK_DIR/package/$pkg"
        cp -r "$pkg" "$SDK_DIR/package/"
        # For openwrt-yun-bridge, verify critical files
        if [ "$pkg" = "openwrt-yun-bridge" ]; then
            for f in bridge_daemon.py yunbridge.init; do
                if [ ! -f "$pkg/$f" ]; then
                    echo "[ERROR] $f missing in $pkg. Aborting build."
                    exit 1
                fi
                if [ ! -f "$SDK_DIR/package/$pkg/$f" ]; then
                    echo "[ERROR] $f failed to copy to SDK/package/$pkg. Aborting build."
                    exit 1
                fi
            done
        fi
    else
        echo "[WARN] Package $pkg not found."
    fi
done

# Ensure OpenWRT SDK detects new packages (refresh package index)
pushd "$SDK_DIR"
# Enable required Yun packages and dependencies automatically
REQUIRED_PKGS="openwrt-yun-bridge openwrt-yun-core luci-app-yunbridge"
REQUIRED_DEPS="python3 python3-pyserial python3-paho-mqtt luci-base luci-compat luci-mod-admin-full lua luci-lib-nixio luci-lib-json python3-aio-mqtt-mod"
CONFIG_CHANGED=0
for pkg in $REQUIRED_PKGS; do
    if ! grep -q "CONFIG_PACKAGE_${pkg}=y" ".config"; then
        echo "CONFIG_PACKAGE_${pkg}=y" >> ".config"
        CONFIG_CHANGED=1
        echo "[INFO] Enabled $pkg in SDK .config."
    fi
done
for dep in $REQUIRED_DEPS; do
    if ! grep -q "CONFIG_PACKAGE_${dep}=y" ".config"; then
        echo "CONFIG_PACKAGE_${dep}=y" >> ".config"
        CONFIG_CHANGED=1
        echo "[INFO] Enabled dependency $dep in SDK .config."
    fi
done
if [ $CONFIG_CHANGED -eq 1 ]; then
    echo "[INFO] Running make defconfig to update package selection..."
    make defconfig
else
    echo "[INFO] Required packages and dependencies already enabled in SDK .config."
fi
popd


# 3. Compilar los paquetes OpenWRT en el SDK
# Limpiar .ipk viejos de openwrt-yun-bridge antes de copiar los nuevos
echo "[CLEANUP] Removing old openwrt-yun-bridge .ipk files from $BIN_DIR..."
find "$BIN_DIR" -type f -name 'openwrt-yun-bridge*_*.ipk' -delete

pushd "$SDK_DIR"
for pkg in luci-app-yunbridge openwrt-yun-core openwrt-yun-bridge; do
    if [ -d "package/$pkg" ]; then
        echo "[BUILD] Building $pkg (.ipk) in SDK..."
        make package/$pkg/clean V=s || true
        make package/$pkg/compile V=s
        # Copiar artefactos .ipk al bin local
        find bin/packages/ -name "$pkg*_*.ipk" -exec cp {} ../$BIN_DIR/ \;
    fi
done
popd




# 4. Compilar openwrt-yun-bridge como .ipk (no .whl)

# openwrt-yun-bridge .ipk is built in the SDK, not locally. Do not run make in the package directory.
if [ -d "openwrt-yun-bridge" ]; then
    echo "[INFO] openwrt-yun-bridge .ipk is built in the SDK. Skipping local make."
else
    echo "[WARN] Package openwrt-yun-bridge not found."
fi


# 5. Compilar openwrt-yun-client-python como .whl
if [ -d "openwrt-yun-client-python" ]; then
    echo "[BUILD] Building openwrt-yun-client-python (.whl) locally..."
    (cd openwrt-yun-client-python && make clean && make wheel)
    cp openwrt-yun-client-python/dist/*.whl "$BIN_DIR/" 2>/dev/null || true
else
    echo "[WARN] Package openwrt-yun-client-python not found."
fi


echo "\n[OK] Build finished. Find the .ipk and .whl artifacts in the bin/ directory."

# Cleanup: remove all 'build', 'bin', 'dist', and '*.egg-info' directories from package folders
echo "[CLEANUP] Removing leftover build, bin, dist, and egg-info directories from packages..."
for pkg in openwrt-yun-bridge openwrt-yun-client-python luci-app-yunbridge openwrt-yun-core; do
    find "$pkg" -type d -name build -exec rm -rf {} +
    find "$pkg" -type d -name bin -exec rm -rf {} +
    find "$pkg" -type d -name dist -exec rm -rf {} +
    find "$pkg" -type d -name '*.egg-info' -exec rm -rf {} +
done
echo "[CLEANUP] Done."
