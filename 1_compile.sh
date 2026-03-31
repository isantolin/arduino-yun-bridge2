#!/bin/bash
set -e
#
# This file is part of Arduino MCU Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# compile.sh - Compila todos los paquetes del ecosistema Arduino MCU v2
# Target: OpenWrt 25.12.0 (APK System)
#

usage() {
    cat <<'EOF'
Usage: ./1_compile.sh [OPTIONS] [OPENWRT_VERSION] [OPENWRT_TARGET]

Options:
  --install-host-deps   Attempt to install missing host dependencies using
                        the system package manager (requires sudo/root).
  --skip-host-deps      Skip dependency installation (default behaviour).
  --quiet               Disable build verbosity (V=s is ON by default).
  -h, --help            Show this message and exit.
EOF
}

# Default to installing host deps unless explicitly disabled
INSTALL_HOST_DEPS=1
VERBOSE=1

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
        --quiet)
            VERBOSE=0
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

# [CONFIG] Target Final OpenWrt 25.12.0
OPENWRT_VERSION=${1:-"25.12.0"}
OPENWRT_TARGET=${2:-"malta/be"}

OPENWRT_URL="https://downloads.openwrt.org/releases/${OPENWRT_VERSION}/targets/${OPENWRT_TARGET}/openwrt-sdk-${OPENWRT_VERSION}-$(echo "$OPENWRT_TARGET" | tr '/' '-')_gcc-14.3.0_musl.Linux-x86_64.tar.zst"
OPENWRT_SHA256_URL="https://downloads.openwrt.org/releases/${OPENWRT_VERSION}/targets/${OPENWRT_TARGET}/sha256sums"

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
# [FIX CRITICO] Rust/Cargo Bridge para CI (GitHub Actions)
# ==============================================================================
# Compilar Rust desde el SDK de OpenWrt toma >1 hora y falla en CI.
# Si rustc y cargo están en el host Y soportan el target, los inyectamos.
inject_rust_into_sdk() {
    local sdk_host_bin="$SDK_DIR/staging_dir/host/bin"
    local sdk_hostpkg_bin="$SDK_DIR/staging_dir/hostpkg/bin"
    local target="mips-unknown-linux-musl"
    local rustup_std_available=1
    
    # Limpiar inyecciones previas para evitar estados inconsistentes
    rm -f "$sdk_host_bin/rustc" "$sdk_host_bin/cargo" "$sdk_host_bin/rustdoc"
    rm -f "$sdk_hostpkg_bin/maturin" "$SDK_DIR/staging_dir/host/stamp/.rust_installed"

    local can_inject=0
    if command -v rustc >/dev/null 2>&1 && command -v cargo >/dev/null 2>&1; then
        # rustup stable does not ship prebuilt std for this Tier-3 target.
        # Skip the futile download attempt and keep the SDK-hosted fallback path.
        case "$target" in
            mips-unknown-linux-musl)
                rustup_std_available=0
                ;;
        esac

        # [FIX] Si rustup está presente, intentamos instalar el target faltante automáticamente
        if command -v rustup >/dev/null 2>&1; then
            if [ "$rustup_std_available" -eq 1 ] && ! rustup target list --installed 2>/dev/null | grep -q "^$target$"; then
                echo "[INFO] Attempting to install missing Rust target $target via rustup..."
                rustup target add "$target" || echo "[WARN] Failed to install Rust target $target via rustup."
            elif [ "$rustup_std_available" -eq 0 ]; then
                echo "[INFO] Skipping rustup target add for $target: stable does not provide prebuilt rust-std."
            fi
        fi

        # Verificar si el target está instalado y es funcional (tiene std)
        if rustup target list --installed 2>/dev/null | grep -q "^$target$"; then
            echo "[INFO] Host Rust fully supports $target. Injecting into SDK..."
            can_inject=1
        else
            echo "[WARN] Host Rust missing 'std' library for $target (Tier 3 target)."
            echo "[INFO] Falling back to SDK-internal Rust build (this will be slow but reliable)."
        fi
    fi

    if [ "$can_inject" -eq 1 ]; then
        mkdir -p "$sdk_host_bin"
        ln -sf "$(command -v rustc)" "$sdk_host_bin/rustc"
        ln -sf "$(command -v cargo)" "$sdk_host_bin/cargo"
        ln -sf "$(command -v rustdoc)" "$sdk_host_bin/rustdoc"
        mkdir -p "$SDK_DIR/staging_dir/host/stamp"
        touch "$SDK_DIR/staging_dir/host/stamp/.rust_installed"
        
        # Inyectar maturin solo si Rust es funcional
        if command -v maturin >/dev/null 2>&1; then
            mkdir -p "$sdk_hostpkg_bin"
            ln -sf "$(command -v maturin)" "$sdk_hostpkg_bin/maturin"
        fi
    fi
}

# [FIX] Compatibilidad Python 3.13 + Rust (PyO3)
export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
# [FIX] Forzar linker BFD para MIPS (evita fallos con el linker por defecto de Rust)
export CARGO_TARGET_MIPS_UNKNOWN_LINUX_MUSL_LINKER="mips-openwrt-linux-musl-gcc"
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

# [FIX] Auto-bootstrap de dependencias externas desde GitHub (PacketSerial)
DUMMY_LIBS="$(pwd)/.dummy_libs"
if [ ! -d "$DUMMY_LIBS/PacketSerial" ]; then
    echo "[INFO] PacketSerial not found. Fetching from GitHub..."
    git clone --depth 1 https://github.com/isantolin/PacketSerial2 "$DUMMY_LIBS/PacketSerial"
else
    echo "[INFO] PacketSerial already present in .dummy_libs."
fi

echo "[INFO] Regenerating protocol files from spec..."
python3 "$REPO_ROOT/tools/protocol/generate.py" \
    --spec "$REPO_ROOT/tools/protocol/spec.toml" \
    --py "$REPO_ROOT/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "$REPO_ROOT/mcubridge-library-arduino/src/protocol/rpc_protocol.h" \
    --cpp-structs "$REPO_ROOT/mcubridge-library-arduino/src/protocol/rpc_structs.h" \
    --py-client "$REPO_ROOT/mcubridge-client-examples/mcubridge_client/protocol.py" || exit 1

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
    MAX_RETRIES=10; RETRY=0; SUCCESS=0
    SDK_FILENAME="$(basename "$OPENWRT_URL")"
    while [ "$RETRY" -lt "$MAX_RETRIES" ]; do
        echo "[INFO] Downloading OpenWRT SDK (attempt $((RETRY+1))/$MAX_RETRIES)..."
        
        # Ensure a clean start by removing any partial/corrupt file
        rm -f sdk.tar.zst

        # Use wget with extreme persistence to overcome network truncation/flakiness
        # --tries=0 (infinite), --timeout=15, --waitretry=5
        if command -v wget >/dev/null 2>&1; then
            echo "[INFO] Using wget with infinite retries for maximum resilience..."
            wget --tries=0 --timeout=15 --waitretry=5 --retry-connrefused -O sdk.tar.zst "$OPENWRT_URL"
        else
            curl -L --http1.1 --retry 10 --retry-delay 5 -o sdk.tar.zst "$OPENWRT_URL"
        fi

        # [SECURITY] SHA256 integrity verification of downloaded SDK
        if wget -qO sha256sums "$OPENWRT_SHA256_URL"; then
            EXPECTED_SHA=$(grep "$SDK_FILENAME" sha256sums | awk '{print $1}')
            rm -f sha256sums
            if [ -n "$EXPECTED_SHA" ]; then
                ACTUAL_SHA=$(sha256sum sdk.tar.zst | awk '{print $1}')
                if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
                    echo "[ERROR] SHA256 mismatch! Expected: $EXPECTED_SHA Got: $ACTUAL_SHA"
                    rm -f sdk.tar.zst
                    RETRY=$((RETRY+1)); sleep 5; continue
                fi
                echo "[INFO] SHA256 verified: $ACTUAL_SHA"
            else
                echo "[WARN] SDK filename not found in sha256sums, skipping verification."
            fi
        else
            echo "[WARN] Could not download sha256sums, skipping verification."
            rm -f sha256sums 2>/dev/null
        fi
        
        echo "[INFO] Extracting SDK..."
        if tar --use-compress-program="${ZSTD_DECOMPRESSOR}" -xf sdk.tar.zst; then
            rm sdk.tar.zst; mv openwrt-sdk-* "$SDK_DIR"; SUCCESS=1;
            # [FIX] Inyectamos Rust antes de cualquier compilación
            inject_rust_into_sdk
            break
        else
            echo "[ERROR] Extraction failed. SDK archive might be corrupt."
            rm -f sdk.tar.zst; rm -rf openwrt-sdk-*; RETRY=$((RETRY+1)); sleep 5
        fi
    done
    [ $SUCCESS -ne 1 ] && { echo "[FATAL] Failed to download and extract OpenWrt SDK after $MAX_RETRIES attempts."; exit 1; }
fi

# [FIX] Asegurar que Rust/Maturin estén inyectados incluso si el SDK ya existía
inject_rust_into_sdk

# [FIX] Leverage host system build tools to avoid slow SDK-internal host-builds
bootstrap_python_module_into_prefix() {
    local target_python="$1"
    local prefix_dir="$2"
    local module_name="$3"
    local pip_spec="$4"

    # [SIL-2] Resilient selection: Use SDK Python if available, fallback to Host Python
    local python_bin
    if [ -x "$target_python" ]; then
        python_bin="$target_python"
    else
        python_bin=$(which python3)
        echo "[INFO] SDK internal Python missing at $target_python, falling back to host Python: $python_bin"
    fi

    echo "[INFO] Bootstrapping $pip_spec in SDK using $python_bin..."
    "$python_bin" -m pip install --no-cache-dir --prefix "$prefix_dir" "$pip_spec" || {
        echo "[WARN] Could not bootstrap $module_name via pip, attempting symlink from host..."
        # Fallback: find host's version and symlink it (Aero-resilient fallback)
        local host_path
        host_path=$(python3 - <<PY 2>/dev/null
import importlib
spec = importlib.import_module("$module_name")
print(getattr(spec, "__file__", ""))
PY
)
        if [ -n "$host_path" ]; then
            mkdir -p "$prefix_dir/lib/python3.13/site-packages"
            ln -sf "$host_path" "$prefix_dir/lib/python3.13/site-packages/"
        fi
    }
}

# Bootstrap critical build tools
bootstrap_python_module_into_prefix "$SDK_DIR/staging_dir/hostpkg/bin/python3" "$SDK_DIR/staging_dir/hostpkg" "hatchling" "hatchling>=1.18.0"
bootstrap_python_module_into_prefix "$SDK_DIR/staging_dir/hostpkg/bin/python3" "$SDK_DIR/staging_dir/hostpkg" "pdm.backend" "pdm-backend>=2.4.0"
bootstrap_python_module_into_prefix "$SDK_DIR/staging_dir/hostpkg/bin/python3" "$SDK_DIR/staging_dir/hostpkg" "maturin" "maturin>=1.4.0"
bootstrap_python_module_into_prefix "$SDK_DIR/staging_dir/hostpkg/bin/python3" "$SDK_DIR/staging_dir/hostpkg" "Cython" "Cython>=3.0.0"

# [FIX] Force bootstrap maturin for cryptography
if [ -x "$SDK_DIR/staging_dir/hostpkg/bin/python3" ]; then
    echo "[INFO] Bootstrapping maturin>=1.4 in SDK..."
    "$SDK_DIR/staging_dir/hostpkg/bin/python3" -m pip install --upgrade --prefix "$SDK_DIR/staging_dir/hostpkg" "maturin>=1.4" || exit 1
fi

# [FIX] Force bootstrap Cython 3.x for uvloop
if [ -x "$SDK_DIR/staging_dir/hostpkg/bin/python3" ]; then
    echo "[INFO] Bootstrapping Cython>=3.1 in SDK..."
    "$SDK_DIR/staging_dir/hostpkg/bin/python3" -m pip install --upgrade --prefix "$SDK_DIR/staging_dir/hostpkg" "Cython>=3.1" || exit 1
fi

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
    if grep -q "src-link mcubridge" "$FEEDS_CONF"; then
        sed -i '/src-link mcubridge/d' "$FEEDS_CONF"
    fi
    
    echo "src-link mcubridge $LOCAL_FEED_PATH" >> "$FEEDS_CONF"
    echo "[INFO] Configured local feed at $LOCAL_FEED_PATH"
    LOCAL_FEED_ENABLED=1
fi

# Fallback: if local feed is NOT enabled, optionally copy package sources into the SDK.
if [ "$LOCAL_FEED_ENABLED" -ne 1 ] && [ "$SYNC_PACKAGES_TO_SDK" -eq 1 ]; then
    for pkg in luci-app-mcubridge mcubridge; do
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
    # [FIX] Pre-emptive cleanup of uboot-ath79 and nginx (recursive dependency in some SDKs)
    [ -d "package/feeds/base/uboot-ath79" ] && rm -rf package/feeds/base/uboot-ath79
    [ -d "package/feeds/packages/nginx" ] && rm -rf package/feeds/packages/nginx
    
    if ./scripts/feeds update -a; then
        SUCCESS=1; break
    else
        rm -rf feeds/base feeds/packages feeds/luci feeds/routing feeds/telephony
        RETRY=$((RETRY+1)); sleep 5
    fi
done
[ $SUCCESS -ne 1 ] && exit 1

# [FIX] SIL-2: Install ONLY required packages to avoid recursive dependency hell 
# in unrelated packages like bigclown, nginx, asterisk, etc.
echo "[INFO] Installing required packages and their dependencies..."
./scripts/feeds install mcubridge luci-app-mcubridge

# ==============================================================================
# [FIX CRITICO] Breaking Kconfig Recursive Dependencies (SDK 25.12.0)
# ==============================================================================
echo "[FIX] Breaking Kconfig recursive dependencies..."

# 1. Break libcurl <-> LIBCURL_LDAP recursion via Makefile patch (Nuclear Option)
LIBCURL_MAKEFILE="feeds/packages/net/curl/Makefile"
if [ -f "$LIBCURL_MAKEFILE" ]; then
    echo "[FIX] Removing LDAP from libcurl Makefile..."
    sed -i 's/+LIBCURL_LDAP:libopenldap//g' "$LIBCURL_MAKEFILE"
fi

# 2. Total Purge to avoid duplicate definitions (python3-click, etc)
echo "[INFO] Purging SDK metadata and redundant feeds..."
rm -rf tmp/ feeds/*.index .config*
# Remove ONLY the problematic symlinks in package/feeds to force fresh install
rm -rf package/feeds/*

# 3. Re-install only the essential feeds to minimize Kconfig surface
echo "[INFO] Re-installing essential feeds..."
./scripts/feeds update -a

# [FIX] Remove duplicate/problematic upstream packages to avoid recursion
echo "[FIX] Removing duplicate upstream packages (click, paho-mqtt, etc)..."
rm -rf feeds/packages/lang/python/python-click
rm -rf feeds/packages/lang/python/python-click-log
rm -rf feeds/packages/lang/python/python-paho-mqtt
rm -rf feeds/packages/lang/python/python-psutil
rm -rf feeds/packages/lang/python/python-cryptography

./scripts/feeds install mcubridge luci-app-mcubridge

# 4. Break python3-click self-selection if it reappeared
CLICK_MAKEFILE="package/feeds/mcubridge/python3-click/Makefile"
if [ -f "$CLICK_MAKEFILE" ]; then
    sed -i 's/select PACKAGE_python3-click//g' "$CLICK_MAKEFILE"
fi
# ==============================================================================

# [FIX] Patch python-uci to include setuptools build dependency (Critical for Python 3.13+)
# ... (rest of patches) ...
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
    echo "[INFO] Installing mcubridge feed overrides..."
    
    # [FIX] Eliminar conflictos de paquetes Python (System vs Local)
    # Estos paquetes existen en el feed oficial 'packages' pero necesitamos las versiones
    # optimizadas o más recientes del feed 'mcubridge'.
    for pkg_conflict in python-paho-mqtt python-cryptography python-psutil; do
        if [ -d "package/feeds/packages/$pkg_conflict" ]; then
            echo "[FIX] Removing upstream $pkg_conflict to prioritize local mcubridge version..."
            rm -rf "package/feeds/packages/$pkg_conflict"
        fi
    done
    
    ./scripts/feeds install -f -p mcubridge -a

    # [FIX CRITICO] Patch python3-structlog para Build Dependencies (Hatch)
    # structlog requiere hatch-fancy-pypi-readme para procesar metadatos del README,
    # lo cual falla en el SDK si la dependencia host no está presente.
    STRUCTLOG_MAKEFILE="package/feeds/mcubridge/python3-structlog/Makefile"
    if [ -f "$STRUCTLOG_MAKEFILE" ]; then
        echo "[FIX] Patching python3-structlog build process in $STRUCTLOG_MAKEFILE..."
        if ! grep -q "Build/Prepare" "$STRUCTLOG_MAKEFILE"; then
            sed -i '/include .*python3-package.mk/a \
\
define Build/Prepare\
	$(call Build/Prepare/Default)\
	# Remove hatch-fancy-pypi-readme from requires list without deleting the property\
	sed -i "s/, \\"hatch-fancy-pypi-readme\\"//g" $(PKG_BUILD_DIR)/pyproject.toml\
	sed -i "s/\\"hatch-fancy-pypi-readme\\", //g" $(PKG_BUILD_DIR)/pyproject.toml\
	sed -i "s/\\"hatch-fancy-pypi-readme\\"//g" $(PKG_BUILD_DIR)/pyproject.toml\
	# Remove the dynamic metadata hook configuration block\
	sed -i "/\\[tool.hatch.metadata.hooks.fancy-pypi-readme\\]/,/\\]/d" $(PKG_BUILD_DIR)/pyproject.toml\
endef' "$STRUCTLOG_MAKEFILE"
        fi
    fi
fi

# ==============================================================================
# [FIX CRITICO] Rust host build on CI (LLVM download-ci-llvm)
# ==============================================================================
# Rust bootstrap (x.py) panics on CI if download-ci-llvm is 'true' or 'if-unchanged'
# without a managed Git repo. Debe ser 'false' para GitHub Actions.
RUST_MAKEFILE="package/feeds/packages/rust/Makefile"
if [ -f "$RUST_MAKEFILE" ]; then
    echo "[FIX] Patching rust host build config for CI..."
    sed -i 's/llvm.download-ci-llvm=true/llvm.download-ci-llvm=false/g' "$RUST_MAKEFILE"
    sed -i 's/llvm.download-ci-llvm=if-unchanged/llvm.download-ci-llvm=false/g' "$RUST_MAKEFILE"
fi
# ==============================================================================

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
REQUIRED_PKGS="mcubridge luci-app-mcubridge"
# [FIX] Dependencias explícitas para asegurar selección en .config.
REQUIRED_DEPS="python3-paho-mqtt python3-aiomqtt python3-tenacity mosquitto-client luaposix"

# [FIX] Forzar limpieza total de metadatos de configuración para evitar errores de recursión
# heredados de escaneos previos del SDK.
rm -rf tmp/ .config*

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

# [FIX] Orden de compilación: Primero librerías críticas (extraídas dinámicamente)
LIBS=$(python3 "$REPO_ROOT/tools/sync_runtime_deps.py" --print-openwrt | grep -vE "^(python3|python3-uci|mosquitto-client|xxd)$" | xargs)
echo "[BUILD] Building libraries: $LIBS..."

# Build all libraries in parallel with as many jobs as cores.
for lib in $LIBS; do
    # Only compile if the package exists in our feeds overlay
    if [ ! -d "$REPO_ROOT/feeds/$lib" ]; then
        echo "[SKIP] $lib is a system package (not in feeds overlay)."
        continue
    fi

    echo "[BUILD] Building library $lib (.apk)..."
    if [ "$VERBOSE" -eq 1 ]; then
        make "package/feeds/mcubridge/$lib/compile" -j$(nproc) V=s || exit 1
    else
        if ! make "package/feeds/mcubridge/$lib/compile" -j$(nproc); then
            echo "[RETRY] Build failed for $lib. Rerunning with -j1 V=s to expose error details..."
            make "package/feeds/mcubridge/$lib/compile" -j1 V=s || exit 1
        fi
    fi
    
    # [FIX] Copiar artefactos .apk de librerías
    find bin/packages/ -name "$lib*.apk" -exec cp {} "$BIN_DIR/" \;
done

# Luego paquetes principales
for pkg in luci-app-mcubridge mcubridge; do
    echo "[BUILD] Building package $pkg (.apk)..."
    if [ "$VERBOSE" -eq 1 ]; then
        make "package/$pkg/compile" -j$(nproc) V=s || exit 1
    else
        if ! make "package/$pkg/compile" -j$(nproc); then
            echo "[RETRY] Build failed for $pkg. Rerunning with -j1 V=s to expose error details..."
            make "package/$pkg/compile" -j1 V=s || exit 1
        fi
    fi

    # [FIX] Copiar artefactos .apk
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
for pkg in mcubridge luci-app-mcubridge; do
    find "$pkg" -type d -name build -exec rm -rf {} +
    find "$pkg" -type d -name bin -exec rm -rf {} +
    find "$pkg" -type d -name dist -exec rm -rf {} +
    find "$pkg" -type d -name '*.egg-info' -exec rm -rf {} +
done
