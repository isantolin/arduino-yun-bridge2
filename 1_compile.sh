#!/bin/bash
set -e
#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# compile.sh - Compila todos los paquetes del ecosistema Arduino Yun v2
# Target: OpenWrt 25.12.0-rc1 (APK System)
#

usage() {
    cat <<'EOF'
Usage: ./1_compile.sh [OPTIONS] [OPENWRT_VERSION] [OPENWRT_TARGET]

Options:
  --install-host-deps   Attempt to install missing host dependencies using
                        the system package manager (requires sudo/root).
  --skip-host-deps      Skip dependency installation (default behaviour).
  -h, --help            Show this message and exit.
EOF
}

# Default to installing host deps unless explicitly disabled
INSTALL_HOST_DEPS=1

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

# [CONFIG] Target Final OpenWrt 25.12.0-rc1
OPENWRT_VERSION=${1:-"25.12.0-rc1"}
OPENWRT_TARGET=${2:-"ath79/generic"}

OPENWRT_URL="downloads.openwrt.org/releases/${OPENWRT_VERSION}/targets/${OPENWRT_TARGET}/openwrt-sdk-${OPENWRT_VERSION}-$(echo "$OPENWRT_TARGET" | tr '/' '-')_gcc-14.3.0_musl.Linux-x86_64.tar.zst"

# Asegurar rutas absolutas
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="$REPO_ROOT/openwrt-sdk"
BIN_DIR="$REPO_ROOT/bin"

sanitize_path() {
    local original_path="$PATH"
    local cleaned=""
    local separator=""
    local modified=0

    IFS=':' read -r -a path_entries <<<"$original_path"
    for entry in "${path_entries[@]}"; do
        if [ -z "$entry" ] || [ "$entry" = "-" ]; then
            modified=1
            continue
        fi
        cleaned+="${separator}${entry}"
        separator=":"
    done

    if [ $modified -eq 1 ]; then
        export PATH="$cleaned"
        echo "[INFO] Removed unsafe PATH entries (blank or '-') for build tooling."
    fi
}

sanitize_path

# ==============================================================================
# [FIX CRITICO] Compatibilidad Python 3.13 + Rust (PyO3)
# ==============================================================================
# Muchas librerías de Python (bcrypt, cryptography) usan una versión de PyO3
# que aún no reconoce oficialmente Python 3.13.
# Esta variable fuerza al compilador Rust a usar la ABI estable (ABI3) y
# permite que la compilación continúe sin errores.
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
# ==============================================================================

# --- HOST DEPENDENCIES ---
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
                    git unzip tar gzip bzip2 xz-utils coreutils libncurses5-dev libncursesw5-dev libreadline-dev \
                    zstd wget python3-docutils libelf-dev libpolkit-agent-1-dev libpolkit-gobject-1-dev \
                    libunwind-dev systemtap-sdt-dev libc6-dev libsysprof-capture-dev \
                    libxcrypt-dev libb2-dev libbz2-dev libgdbm-dev libnsl-dev tk-dev tcl-dev \
                    uuid-dev liblzma-dev libbluetooth-dev libbsd-dev binutils-dev asciidoctor \
                    g++-multilib gcc-mingw-w64-x86-64 binutils-mingw-w64-x86-64
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
                    git unzip tar gzip bzip2 xz coreutils ncurses-devel readline-devel zstd wget \
                    python3-docutils elfutils-libelf-devel elfutils-devel polkit-devel \
                    libunwind-devel systemtap-sdt-devel glibc-devel sysprof-devel \
                    libxcrypt-devel libb2-devel bzip2-devel gdbm-devel libnsl2-devel \
                    tk-devel tcl-devel libuuid-devel xz-devel \
                    bluez-libs-devel libbsd-devel binutils-devel asciidoctor \
                    glibc-devel.i686 libstdc++-devel.i686 \
                    mingw64-gcc mingw64-binutils
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

# --- PROTOCOL & DEPS SYNC ---
echo "[INFO] Synchronizing runtime dependency manifests..."
python3 "$REPO_ROOT/tools/sync_runtime_deps.py" || exit 1

echo "[INFO] Regenerating protocol files from spec..."
python3 "$REPO_ROOT/tools/protocol/generate.py" \
    --spec "$REPO_ROOT/tools/protocol/spec.toml" \
    --py "$REPO_ROOT/openwrt-yun-bridge/yunbridge/rpc/protocol.py" \
    --cpp "$REPO_ROOT/openwrt-library-arduino/src/protocol/rpc_protocol.h" || exit 1

# --- BOOTSTRAP PYTHON CHECKS ---
auto_install_python_module() {
    local module="$1"
    local package="python3-${module}"
    local prefix=()
    if [ "$INSTALL_HOST_DEPS" != "1" ]; then return 1; fi
    if [ "$EUID" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then prefix=(sudo); else return 1; fi
    fi
    if [ -f /etc/debian_version ]; then
        "${prefix[@]}" apt-get install -y "$package" && return 0
    fi
    return 1
}

check_python_module() {
    local module="$1"
    python3 -c "import ${module}" >/dev/null 2>&1 && return 0
    if auto_install_python_module "$module" && python3 -c "import ${module}" >/dev/null 2>&1; then return 0; fi
    echo "[ERROR] Missing required Python module '${module}'." >&2
    exit 1
}

check_python_module "setuptools"

# ... (Funciones bootstrap auxiliares simplificadas para ejecución) ...
# Para asegurar éxito, incluimos lógica básica de bootstrap aquí si falla el entorno
bootstrap_python_module_into_prefix() {
    local python_bin="$1"
    local prefix_dir="$2"
    local module="$3"
    local package_spec="${4:-$module}"
    if [ -x "$python_bin" ]; then
        if ! "$python_bin" -c "import ${module}" >/dev/null 2>&1; then
            echo "[INFO] Bootstrapping $module in SDK..."
            if ! "$python_bin" -m pip install --upgrade --prefix "$prefix_dir" "$package_spec"; then
                echo "[ERROR] Failed to bootstrap $module"
                exit 1
            fi
        fi
    fi
}

# --- PREPARE SDK ---
if command -v unzstd >/dev/null 2>&1; then ZSTD_DECOMPRESSOR="unzstd"; 
elif command -v zstd >/dev/null 2>&1; then ZSTD_DECOMPRESSOR="zstd -d"; 
else echo "[ERROR] zstd not found."; exit 1; fi

echo "[INFO] Preparing build environment..."
mkdir -p "$BIN_DIR"

if [ -d "$SDK_DIR" ] && [ ! -f "$SDK_DIR/scripts/feeds" ]; then
    rm -rf "$SDK_DIR"
fi

if [ ! -d "$SDK_DIR" ]; then
    MAX_RETRIES=5; RETRY=0; SUCCESS=0
    while [ "$RETRY" -lt "$MAX_RETRIES" ]; do
        echo "[INFO] Downloading OpenWRT SDK (attempt $((RETRY+1))/$MAX_RETRIES)..."
        wget -O sdk.tar.zst "$OPENWRT_URL"
        if tar --use-compress-program="${ZSTD_DECOMPRESSOR}" -xf sdk.tar.zst; then
            rm sdk.tar.zst; mv openwrt-sdk-* "$SDK_DIR"; SUCCESS=1; break
        else
            rm -f sdk.tar.zst; rm -rf openwrt-sdk-*; RETRY=$((RETRY+1)); sleep 2
        fi
    done
    [ $SUCCESS -ne 1 ] && exit 1
fi

# Bootstrap build deps inside SDK
bootstrap_python_module_into_prefix "$SDK_DIR/staging_dir/hostpkg/bin/python3" "$SDK_DIR/staging_dir/hostpkg" "hatchling" "hatchling==1.18.0"

# 2. Package Sources
# Prefer using the local feed (src-link) to avoid duplicated/copies drifting.
# If you really need to copy sources directly into the SDK tree, set:
#   SYNC_PACKAGES_TO_SDK=1
SYNC_PACKAGES_TO_SDK="${SYNC_PACKAGES_TO_SDK:-0}"

# --- FEEDS SETUP (FIXED FLAT STRUCTURE) ---
LOCAL_FEED_ENABLED=0
# [FIX] Ahora apunta a feeds/ directamente (estructura plana)
LOCAL_FEED_PATH="$REPO_ROOT/feeds"

if [ -d "$LOCAL_FEED_PATH" ]; then
    # Sync overlay first
    [ -x "$REPO_ROOT/tools/sync_feed_overlay.sh" ] && "$REPO_ROOT/tools/sync_feed_overlay.sh" --dest "$LOCAL_FEED_PATH"
    
    FEEDS_CONF="$SDK_DIR/feeds.conf"
    [ ! -f "$FEEDS_CONF" ] && cp "$SDK_DIR/feeds.conf.default" "$FEEDS_CONF"

    # [OPTIMIZATION] Aplicar Mirrors de GitHub para velocidad y estabilidad
    echo "[INFO] Switching feeds to GitHub mirrors..."
    sed -i 's|https://git.openwrt.org/openwrt/openwrt.git|https://github.com/openwrt/openwrt.git|g' "$FEEDS_CONF"
    sed -i 's|https://git.openwrt.org/feed/packages.git|https://github.com/openwrt/packages.git|g' "$FEEDS_CONF"
    sed -i 's|https://git.openwrt.org/project/luci.git|https://github.com/openwrt/luci.git|g' "$FEEDS_CONF"
    sed -i 's|https://git.openwrt.org/feed/routing.git|https://github.com/openwrt/routing.git|g' "$FEEDS_CONF"
    sed -i 's|https://git.openwrt.org/feed/telephony.git|https://github.com/openwrt/telephony.git|g' "$FEEDS_CONF"
    
    # [FIX] Limpiar configuración antigua para forzar la ruta nueva
    if grep -q "src-link yunbridge" "$FEEDS_CONF"; then
        sed -i '/src-link yunbridge/d' "$FEEDS_CONF"
    fi
    
    echo "src-link yunbridge $LOCAL_FEED_PATH" >> "$FEEDS_CONF"
    echo "[INFO] Configured local feed at $LOCAL_FEED_PATH"
    LOCAL_FEED_ENABLED=1
fi

# Fallback: if local feed is NOT enabled, optionally copy package sources into the SDK.
if [ "$LOCAL_FEED_ENABLED" -ne 1 ] && [ "$SYNC_PACKAGES_TO_SDK" -eq 1 ]; then
    for pkg in luci-app-yunbridge openwrt-yun-core openwrt-yun-bridge; do
        if [ -d "$pkg" ]; then
            echo "[INFO] Syncing $pkg to SDK..."
            rm -rf "$SDK_DIR/package/$pkg"
            cp -r "$pkg" "$SDK_DIR/package/"
        fi
    done
fi

# Update Feeds
cd "$SDK_DIR" || exit 1
MAX_RETRIES=5; RETRY=0; SUCCESS=0
while [ "$RETRY" -lt "$MAX_RETRIES" ]; do
    # [FIX] Pre-emptive cleanup of uboot-ath79
    [ -d "package/feeds/base/uboot-ath79" ] && rm -rf package/feeds/base/uboot-ath79
    
    if ./scripts/feeds update -a; then
        SUCCESS=1; break
    else
        rm -rf feeds/base feeds/packages feeds/luci feeds/routing feeds/telephony
        RETRY=$((RETRY+1)); sleep 5
    fi
done
[ $SUCCESS -ne 1 ] && exit 1

# Install Feeds & Manage Conflicts
echo "[INFO] Installing feeds..."
./scripts/feeds install -a

# [FIX] Patch python-uci to include setuptools build dependency (Critical for Python 3.13+)
PYTHON_UCI_MAKEFILE="package/feeds/packages/python-uci/Makefile"
if [ -f "$PYTHON_UCI_MAKEFILE" ]; then
    echo "[FIX] Patching python-uci build dependencies in $PYTHON_UCI_MAKEFILE..."
    if ! grep -q "PKG_BUILD_DEPENDS:=python3-setuptools" "$PYTHON_UCI_MAKEFILE"; then
        sed -i '/PKG_SOURCE_VERSION:=/a PKG_BUILD_DEPENDS:=python3-setuptools/host python3-build/host' "$PYTHON_UCI_MAKEFILE"
    fi
else 
    echo "[WARN] python-uci Makefile not found at $PYTHON_UCI_MAKEFILE"
fi

# ==============================================================================
# [FIX CRITICO] Patch python-cryptography para Cross-Compilation
# ==============================================================================
PYTHON_CRYPTO_MAKEFILE="package/feeds/packages/python-cryptography/Makefile"
if [ -f "$PYTHON_CRYPTO_MAKEFILE" ]; then
    echo "[FIX] Patching python-cryptography build flags in $PYTHON_CRYPTO_MAKEFILE..."
    if ! grep -q "TARGET_CFLAGS += -I\$(STAGING_DIR)" "$PYTHON_CRYPTO_MAKEFILE"; then
        sed -i '/include .*python3-package.mk/a TARGET_CFLAGS += -I$(STAGING_DIR)/usr/include/python$(PYTHON3_VERSION)' "$PYTHON_CRYPTO_MAKEFILE"
    fi
else
    echo "[WARN] python-cryptography Makefile not found at $PYTHON_CRYPTO_MAKEFILE"
fi

# ==============================================================================
# [FIX CRITICO] Patch python-pyopenssl para Wheel Name Mismatch
# ==============================================================================
# PyOpenSSL genera un wheel en minúsculas (pyopenssl-*.whl), pero OpenWrt
# espera MixedCase (pyOpenSSL-*.whl) basado en el nombre de PyPI.
# Este parche fuerza el nombre esperado a minúsculas.
PYTHON_OPENSSL_MAKEFILE="package/feeds/packages/python-pyopenssl/Makefile"
if [ -f "$PYTHON_OPENSSL_MAKEFILE" ]; then
    echo "[FIX] Patching python-pyopenssl wheel name in $PYTHON_OPENSSL_MAKEFILE..."
    if ! grep -q "PYTHON3_PKG_WHEEL_NAME:=pyopenssl" "$PYTHON_OPENSSL_MAKEFILE"; then
        # Insertamos la redefinición después de PKG_NAME
        sed -i '/PKG_NAME:=/a PYTHON3_PKG_WHEEL_NAME:=pyopenssl' "$PYTHON_OPENSSL_MAKEFILE"
    fi
else
    echo "[WARN] python-pyopenssl Makefile not found at $PYTHON_OPENSSL_MAKEFILE"
fi
# ==============================================================================


if [ $LOCAL_FEED_ENABLED -eq 1 ]; then
    echo "[INFO] Installing yunbridge feed overrides..."
    
    # [FIX] Eliminar conflicto Paho MQTT (System vs Local)
    if [ -d "package/feeds/packages/python-paho-mqtt" ]; then
        echo "[FIX] Removing upstream python-paho-mqtt (v1.6) to prioritize local yunbridge version (v2.1)..."
        rm -rf package/feeds/packages/python-paho-mqtt
    fi
    
    ./scripts/feeds install -f -p yunbridge -a
fi

# [FIX] Cleanup uboot again
[ -d "package/feeds/base/uboot-ath79" ] && rm -rf package/feeds/base/uboot-ath79

# Apply Overlays
FEEDS_PACKAGES_OVERLAY_DIR="$REPO_ROOT/openwrt-overlays/feeds/packages"
if [ -d "$FEEDS_PACKAGES_OVERLAY_DIR" ]; then
    mkdir -p feeds/packages
    cp -a "$FEEDS_PACKAGES_OVERLAY_DIR/." feeds/packages/
fi

# Kernel Stubs Stripping (para evitar warnings)
USB_MODULES_MK="package/kernel/linux/modules/usb.mk"
if [ -f "$USB_MODULES_MK" ]; then
    if grep -q "kmod-phy-bcm-ns-usb" "$USB_MODULES_MK"; then
        sed -i '/kmod-phy-bcm-ns-usb2/d' "$USB_MODULES_MK"
        sed -i '/kmod-phy-bcm-ns-usb3/d' "$USB_MODULES_MK"
    fi
fi

# Enable Packages
REQUIRED_PKGS="openwrt-yun-bridge openwrt-yun-core luci-app-yunbridge"
# [FIX] Dependencias explícitas para asegurar selección en .config.
# Se ELIMINÓ python3-twisted porque prometheus_client ha sido optimizado para no usarlo.
REQUIRED_DEPS="python3-paho-mqtt python3-aiomqtt mosquitto-client luaposix"

for pkg in $REQUIRED_PKGS $REQUIRED_DEPS; do
    if ! grep -q "CONFIG_PACKAGE_${pkg}=y" ".config"; then
        echo "CONFIG_PACKAGE_${pkg}=y" >> ".config"
    fi
done
make defconfig

# 3. Compilation
echo "[CLEANUP] Removing old .apk files..."
find "$BIN_DIR" -type f -name '*.apk' -delete

# [FIX] Asegurar que estamos en el SDK antes de compilar
cd "$SDK_DIR" || { echo "[ERROR] Cannot enter SDK dir $SDK_DIR"; exit 1; }

# [FIX] Orden de compilación: Primero librerías críticas
# Nota: Ahora están en el feed 'yunbridge' que apunta a 'feeds/' plano
for lib in python3-paho-mqtt python3-aiomqtt python3-cobs python3-prometheus-client; do
    echo "[BUILD] Building library $lib..."
    make package/feeds/yunbridge/$lib/compile V=s
    
    # [FIX] Copiar artefactos .apk de librerías
    find bin/packages/ -name "$lib*.apk" -exec cp {} "$BIN_DIR/" \;
done

# Luego paquetes principales
for pkg in luci-app-yunbridge openwrt-yun-core openwrt-yun-bridge; do
    echo "[BUILD] Building package $pkg (.apk)..."
    make package/$pkg/clean V=s || true
    make package/$pkg/compile V=s
    
    # [FIX] Copiar artefactos .apk (Patrón corregido para formato APK: nombre-ver-rel.apk)
    find bin/packages/ -name "$pkg*.apk" -exec cp {} "$BIN_DIR/" \;
done
cd "$REPO_ROOT" || exit 1

# Checksums
if ls "$BIN_DIR"/*.apk >/dev/null 2>&1; then
    if command -v sha256sum >/dev/null 2>&1; then
        echo "[INFO] Generating SHA256SUMS manifest..."
        (cd "$BIN_DIR" && sha256sum *.apk > SHA256SUMS)
    fi
else
    echo "[WARN] No .apk artifacts detected in $BIN_DIR."
fi

echo "\n[OK] Build finished. Check the bin/ directory."

# Cleanup
for pkg in openwrt-yun-bridge luci-app-yunbridge openwrt-yun-core; do
    find "$pkg" -type d -name build -exec rm -rf {} +
    find "$pkg" -type d -name bin -exec rm -rf {} +
    find "$pkg" -type d -name dist -exec rm -rf {} +
    find "$pkg" -type d -name '*.egg-info' -exec rm -rf {} +
done
