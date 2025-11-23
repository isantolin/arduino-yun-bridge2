#!/bin/bash
set -e
#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# This program is free software: you can redistribute and/or modify
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
# Uso: ./compile.sh [--install-host-deps] [--skip-host-deps] [VERSION] [TARGET]
#
# Flags (optional):
#   --install-host-deps   Ejecuta la instalación automática de dependencias
#                         del host mediante apt/dnf si es posible.
#   --skip-host-deps      Fuerza la omisión de instalación automática incluso
#                         si YUNBRIDGE_INSTALL_HOST_DEPS=1.
#   -h, --help            Muestra esta ayuda y termina.

usage() {
    cat <<'EOF'
Usage: ./1_compile.sh [OPTIONS] [OPENWRT_VERSION] [OPENWRT_TARGET]

Options:
  --install-host-deps   Attempt to install missing host dependencies using
                        the system package manager (requires sudo/root).
  --skip-host-deps      Skip dependency installation (default behaviour).
  -h, --help            Show this message and exit.

Environment variables:
  YUNBRIDGE_INSTALL_HOST_DEPS=1  enables host dependency installation.
  YUNBRIDGE_SKIP_HOST_DEPS=1     forces skip regardless of other flags.
    YUNBRIDGE_SERIAL_RETRY_TIMEOUT overrides the default serial ACK timeout
                                                                 picked up later by 3_install.sh.
    YUNBRIDGE_SERIAL_RETRY_ATTEMPTS overrides retry attempts consumed by
                                                                    3_install.sh when initialising UCI.
EOF
}

INSTALL_HOST_DEPS=${YUNBRIDGE_INSTALL_HOST_DEPS:-0}
if [ "${YUNBRIDGE_SKIP_HOST_DEPS:-0}" = "1" ]; then
    INSTALL_HOST_DEPS=0
fi

POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-host-deps)
            INSTALL_HOST_DEPS=1
            shift
            ;;
        --skip-host-deps)
            INSTALL_HOST_DEPS=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            while [[ $# -gt 0 ]]; do
                POSITIONAL+=("$1")
                shift
            done
            break
            ;;
        -* )
            echo "[ERROR] Unknown option: $1" >&2
            usage
            exit 1
            ;;
        * )
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

set -- "${POSITIONAL[@]}"

# Default OpenWRT version and target, can be overridden by the first and second arguments
OPENWRT_VERSION=${1:-"24.10.4"}
OPENWRT_TARGET=${2:-"ath79/generic"}

OPENWRT_URL="https://downloads.openwrt.org/releases/${OPENWRT_VERSION}/targets/${OPENWRT_TARGET}/openwrt-sdk-${OPENWRT_VERSION}-$(echo "$OPENWRT_TARGET" | tr '/' '-')_gcc-13.3.0_musl.Linux-x86_64.tar.zst"
SDK_DIR="openwrt-sdk"
BIN_DIR="bin"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$INSTALL_HOST_DEPS" = "1" ]; then
    echo "[INFO] Host dependency auto-install enabled."
    if [ "$(uname -s)" = "Linux" ]; then
        if [ -f /etc/debian_version ]; then
            if [ "$EUID" -ne 0 ]; then
                if command -v sudo >/dev/null 2>&1; then
                    PKG_PREFIX=(sudo)
                else
                    echo "[WARN] sudo not found and not running as root; skipping automatic apt-get install."
                    PKG_PREFIX=()
                fi
            else
                PKG_PREFIX=()
            fi

            if [ ${#PKG_PREFIX[@]} -ne 0 ] || [ "$EUID" -eq 0 ]; then
                echo "[INFO] Installing packages for Ubuntu/Debian..."
                "${PKG_PREFIX[@]}" apt-get update
                "${PKG_PREFIX[@]}" apt-get install -y \
                    build-essential python3 python3-pip python3-setuptools python3-wheel python3-build python3-hatchling \
                    git unzip tar gzip bzip2 xz-utils coreutils libncurses5-dev libncursesw5-dev \
                    zstd wget python3-docutils libelf-dev libpolkit-agent-1-dev libpolkit-gobject-1-dev \
                    libunwind-dev systemtap-sdt-dev libc6-dev libsysprof-capture-dev \
                    libxcrypt-dev libb2-dev libbz2-dev libgdbm-dev libnsl-dev tk-dev tcl-dev \
                    uuid-dev libsqlite3-dev liblzma-dev libbluetooth-dev libbsd-dev binutils-dev asciidoctor \
                    g++-multilib
            fi
        elif [ -f /etc/fedora-release ]; then
            if [ "$EUID" -ne 0 ]; then
                if command -v sudo >/dev/null 2>&1; then
                    PKG_PREFIX=(sudo)
                else
                    echo "[WARN] sudo not found and not running as root; skipping automatic dnf install."
                    PKG_PREFIX=()
                fi
            else
                PKG_PREFIX=()
            fi

            if [ ${#PKG_PREFIX[@]} -ne 0 ] || [ "$EUID" -eq 0 ]; then
                echo "[INFO] Installing packages for Fedora..."
                "${PKG_PREFIX[@]}" dnf install -y \
                    make automake gcc gcc-c++ kernel-devel \
                    python3 python3-pip python3-setuptools python3-wheel python3-build python3-hatchling \
                    git unzip tar gzip bzip2 xz coreutils ncurses-devel zstd wget \
                    python3-docutils elfutils-libelf-devel elfutils-devel polkit-devel \
                    libunwind-devel systemtap-sdt-devel glibc-devel sysprof-devel \
                    libxcrypt-devel libb2-devel bzip2-devel gdbm-devel libnsl2-devel \
                    tk-devel tcl-devel libuuid-devel sqlite-devel xz-devel \
                    bluez-libs-devel libbsd-devel binutils-devel asciidoctor \
                    glibc-devel.i686 libstdc++-devel.i686
            fi
        else
            echo "[WARN] Unrecognized Linux distro. Please install build-essential equivalents manually."
        fi
    else
        echo "[WARN] Operating system not supported for automatic dependency installation."
    fi
else
    echo "[INFO] Host dependency auto-install disabled. Ensure prerequisites are installed or rerun with --install-host-deps."
fi

REQUIRED_COMMANDS=(wget tar python3 git)
for cmd in "${REQUIRED_COMMANDS[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "[ERROR] Required command '$cmd' not found in PATH. Install it or rerun with --install-host-deps." >&2
        exit 1
    fi
done

auto_install_python_module() {
    local module="$1"
    local package="python3-${module}"
    local prefix=()

    if [ "$INSTALL_HOST_DEPS" != "1" ]; then
        return 1
    fi

    if [ "$EUID" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            prefix=(sudo)
        else
            echo "[WARN] sudo not found; cannot auto-install ${package}." >&2
            return 1
        fi
    fi

    if [ -f /etc/debian_version ]; then
        echo "[INFO] Installing ${package} via apt-get..."
        if "${prefix[@]}" apt-get install -y "$package"; then
            return 0
        fi
    elif [ -f /etc/fedora-release ]; then
        echo "[INFO] Installing ${package} via dnf..."
        if "${prefix[@]}" dnf install -y "$package"; then
            return 0
        fi
    fi

    echo "[WARN] Automatic installation failed for ${package}." >&2
    return 1
}

check_python_module() {
    local module="$1"
    if python3 -c "import ${module}" >/dev/null 2>&1; then
        return 0
    fi

    if auto_install_python_module "$module" && python3 -c "import ${module}" >/dev/null 2>&1; then
        return 0
    fi

    echo "[ERROR] Missing required Python module '${module}'." >&2
    if [ -f /etc/debian_version ]; then
        echo "[HINT] Install it via: sudo apt-get install python3-${module}" >&2
    elif [ -f /etc/fedora-release ]; then
        echo "[HINT] Install it via: sudo dnf install python3-${module}" >&2
    else
        echo "[HINT] Install the python3-${module} package using your distro's package manager." >&2
    fi
    exit 1
}

check_python_module "setuptools"

bootstrap_sdk_python_module() {
    local module="$1"
    local host_python="$SDK_DIR/staging_dir/host/bin/python3"
    local host_prefix="$SDK_DIR/staging_dir/host"

    if [ ! -x "$host_python" ]; then
        echo "[WARN] SDK host python not found at $host_python; skip auto-install for ${module} until the toolchain is prepared." >&2
        return 0
    fi

    if "$host_python" -c "import ${module}" >/dev/null 2>&1; then
        return 0
    fi

    echo "[INFO] Installing ${module} inside the OpenWrt SDK host python..."

    if ! "$host_python" -m pip --version >/dev/null 2>&1; then
        if ! "$host_python" -m ensurepip --upgrade; then
            echo "[ERROR] Failed to bootstrap pip inside the SDK host python." >&2
            return 1
        fi
    fi

    if "$host_python" -m pip install --upgrade --prefix "$host_prefix" "$module"; then
        return 0
    fi

    echo "[ERROR] Unable to install ${module} into the SDK host python." >&2
    return 1
}

if command -v unzstd >/dev/null 2>&1; then
    ZSTD_DECOMPRESSOR="unzstd"
elif command -v zstd >/dev/null 2>&1; then
    ZSTD_DECOMPRESSOR="zstd -d"
else
    echo "[ERROR] Neither 'unzstd' nor 'zstd' is available. Install zstd package." >&2
    exit 1
fi


echo "[INFO] Regenerating protocol files from spec..."
if ! python3 "$REPO_ROOT/tools/protocol/generate.py"; then
    echo "[ERROR] Failed to regenerate protocol files. Aborting." >&2
    exit 1
fi

echo "[INFO] Preparing build environment..."
mkdir -p "$BIN_DIR"

# 1. Download and extract the buildroot/SDK if it does not exist, with retry logic for data corruption
if [ -d "$SDK_DIR" ] && [ ! -f "$SDK_DIR/scripts/feeds" ]; then
    echo "[WARN] Incomplete SDK detected. Removing and re-downloading."
    rm -rf "$SDK_DIR"
fi

if [ ! -d "$SDK_DIR" ]; then
    MAX_RETRIES=5
    RETRY=0
    SUCCESS=0
    while [ $RETRY -lt $MAX_RETRIES ]; do
        RETRY_COUNT=$(expr $RETRY + 1)
        echo "[INFO] Downloading OpenWRT SDK (attempt $RETRY_COUNT/$MAX_RETRIES)..."
        wget -O sdk.tar.zst "$OPENWRT_URL"
        echo "[INFO] Extracting SDK..."
        if tar --use-compress-program="${ZSTD_DECOMPRESSOR}" -xf sdk.tar.zst; then
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

if ! bootstrap_sdk_python_module "setuptools"; then
    echo "[ERROR] SDK host python is missing setuptools even after an install attempt." >&2
    exit 1
fi

# 2. Copy OpenWRT packages to buildroot/SDK (after feeds are updated and luci-base is installed)

# Always copy latest package sources into SDK/package (prevents stale/missing package errors)


# Always copy latest package sources into SDK/package (prevents stale/missing package errors)

for pkg in python3-cobs python3-pyserial-asyncio python3-aiomqtt python3-paho-mqtt python3-tenacity luci-app-yunbridge openwrt-yun-core openwrt-yun-bridge; do
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
echo "[INFO] Updating feeds..."
MAX_RETRIES=5
RETRY=0
SUCCESS=0
while [ $RETRY -lt $MAX_RETRIES ]; do
    RETRY_COUNT=$(expr $RETRY + 1)
    echo "[INFO] Updating feeds (attempt $RETRY_COUNT/$MAX_RETRIES)..."
    if ./scripts/feeds update -a; then
        SUCCESS=1
        break
    else
        echo "[WARN] Feeds update failed. Retrying..."
        RETRY=$(expr $RETRY + 1)
        sleep 2
    fi
done
if [ $SUCCESS -ne 1 ]; then
    echo "[ERROR] Failed to update feeds after $MAX_RETRIES attempts. Exiting."
    exit 1
fi
echo "[INFO] Installing feeds..."
./scripts/feeds install -a

# Remove feed-provided python-paho-mqtt to avoid duplicate Kconfig entries.
if [ -d "package/feeds/packages/python-paho-mqtt" ]; then
    echo "[INFO] Removing feed copy of python-paho-mqtt (overridden locally)."
    rm -rf package/feeds/packages/python-paho-mqtt
fi

# The SDK for ath79 omits bcm53xx PHY modules, strip the dangling deps to avoid warnings.
USB_MODULES_MK="package/kernel/linux/modules/usb.mk"
if [ -f "$USB_MODULES_MK" ]; then
    if grep -q "kmod-phy-bcm-ns-usb" "$USB_MODULES_MK"; then
        echo "[INFO] Removing references to missing BCM Northstar USB PHY kmods..."
        sed -i '/kmod-phy-bcm-ns-usb2/d' "$USB_MODULES_MK"
        sed -i '/kmod-phy-bcm-ns-usb3/d' "$USB_MODULES_MK"
    fi
fi

# Enable required Yun packages and dependencies automatically
MANIFEST_DEPS="$(python3 "$REPO_ROOT/tools/sync_runtime_deps.py" --print-openwrt | paste -sd ' ' -)"
if [ -z "$MANIFEST_DEPS" ]; then
    echo "[ERROR] Unable to collect runtime dependencies from dependencies/runtime.toml" >&2
    exit 1
fi
REQUIRED_PKGS="python3-cobs python3-pyserial-asyncio python3-aiomqtt python3-paho-mqtt python3-tenacity openwrt-yun-bridge openwrt-yun-core luci-app-yunbridge"
REQUIRED_DEPS="${MANIFEST_DEPS} mosquitto-client luaposix"
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
for pkg in python3-cobs python3-pyserial-asyncio python3-aiomqtt python3-tenacity luci-app-yunbridge openwrt-yun-core openwrt-yun-bridge; do
    echo "[BUILD] Building $pkg (.ipk) in SDK..."
    make package/$pkg/clean V=s || true
    make package/$pkg/compile V=s
    # Copiar artefactos .ipk al bin local
    find bin/packages/ -name "$pkg*_*.ipk" -exec cp {} ../$BIN_DIR/ \;
done
popd


if ls "$BIN_DIR"/*.ipk >/dev/null 2>&1; then
    if command -v sha256sum >/dev/null 2>&1; then
        echo "[INFO] Generating SHA256SUMS manifest in $BIN_DIR..."
        (cd "$BIN_DIR" && sha256sum *.ipk > SHA256SUMS)
    else
        echo "[WARN] sha256sum command not found; skipping checksum manifest generation."
    fi
else
    echo "[WARN] No .ipk artifacts detected in $BIN_DIR; skipping SHA256SUMS generation."
fi




# 4. Compilar openwrt-yun-bridge como .ipk (no .whl)

# openwrt-yun-bridge .ipk is built in the SDK, not locally. Do not run make in the package directory.
if [ -d "openwrt-yun-bridge" ]; then
    echo "[INFO] openwrt-yun-bridge .ipk is built in the SDK. Skipping local make."
else
    echo "[WARN] Package openwrt-yun-bridge not found."
fi





echo "\n[OK] Build finished. Find the .ipk and .whl artifacts in the bin/ directory."
echo "[HINT] Antes de ejecutar 3_install.sh puedes exportar"
echo "       YUNBRIDGE_SERIAL_RETRY_TIMEOUT / YUNBRIDGE_SERIAL_RETRY_ATTEMPTS"
echo "       para personalizar el control de flujo serie por defecto."

# Cleanup: remove all 'build', 'bin', 'dist', and '*.egg-info' directories from package folders
echo "[CLEANUP] Removing leftover build, bin, dist, and egg-info directories from packages..."
for pkg in openwrt-yun-bridge luci-app-yunbridge openwrt-yun-core; do
    find "$pkg" -type d -name build -exec rm -rf {} +
    find "$pkg" -type d -name bin -exec rm -rf {} +
    find "$pkg" -type d -name dist -exec rm -rf {} +
    find "$pkg" -type d -name '*.egg-info' -exec rm -rf {} +
done
echo "[CLEANUP] Done."