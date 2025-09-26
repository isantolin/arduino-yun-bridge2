#!/bin/bash
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
        sudo dnf clean all
        sudo dnf update
        sudo dnf install -y @development-tools python3 python3-pip python3-setuptools python3-wheel python3-build git unzip tar gzip bzip2 xz coreutils ncurses-devel zstd wget
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
        echo "[INFO] Downloading OpenWRT SDK (attempt $((RETRY+1))/$MAX_RETRIES)..."
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
            RETRY=$((RETRY+1))
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
for pkg in luci-app-yunbridge openwrt-yun-core openwrt-yun-bridge; do
    if [ -d "$pkg" ]; then
        echo "[INFO] Syncing $pkg to SDK..."
        rm -rf "$SDK_DIR/package/$pkg"
        cp -r "$pkg" "$SDK_DIR/package/"
    else
        echo "[WARN] Package $pkg not found."
    fi
done

# Ensure OpenWRT SDK detects new packages (refresh package index)
pushd "$SDK_DIR"
echo "[INFO] Running make defconfig to refresh package index..."
make defconfig
popd

# 3. Compilar los paquetes OpenWRT en el SDK
pushd "$SDK_DIR"
for pkg in luci-app-yunbridge openwrt-yun-core; do
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
if [ -d "openwrt-yun-bridge" ]; then
    echo "[BUILD] Building openwrt-yun-bridge (.ipk) locally..."
    (cd openwrt-yun-bridge && make clean)
    # El .ipk se genera en el SDK, no localmente
else
    echo "[WARN] Package openwrt-yun-bridge not found."
fi


# 5. Compilar openwrt-yun-client-python como .whl
if [ -d "openwrt-yun-client-python" ]; then
    echo "[BUILD] Building openwrt-yun-client-python (.whl) locally..."
    (cd openwrt-yun-client-python && make clean && make wheel)
    cp openwrt-yun-client-python/bin/*.whl "$BIN_DIR/" 2>/dev/null || true
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
