#!/bin/bash
#
# McuBridge Arduino library install script - Robust version [SIL-2]
# This script installs the McuBridge library and its dependencies into the 
# Arduino libraries directory.

set -e
set -u

# Always work relative to the script location
# SCRIPT_DIR is .../openwrt-library-arduino/tools
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# LIB_ROOT is .../openwrt-library-arduino
LIB_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "================================================================================"
echo " McuBridge Arduino Library Installer"
echo "================================================================================"

# --- Path Detection ---
get_arduino_lib_dir() {
    # 1. Manual override
    if [ "${1:-}" != "" ]; then
        echo "$1"
        return
    fi

    # 2. OS-specific defaults
    case "$(uname)" in
        Darwin)
            echo "$HOME/Documents/Arduino/libraries"
            ;;
        Linux)
            if [ -d "$HOME/Documents/Arduino/libraries" ]; then
                echo "$HOME/Documents/Arduino/libraries"
            else
                echo "$HOME/Arduino/libraries"
            fi
            ;;
        *)
            echo "$HOME/Arduino/libraries"
            ;;
    esac
}

LIB_DIR=$(get_arduino_lib_dir "${1:-}")
echo "[INFO] Target directory: $LIB_DIR"

mkdir -p "$LIB_DIR"

# --- Dependency Management ---
download_zip() {
    local name=$1
    local url=$2
    local dest=$3

    if [ -f "$dest" ]; then
        return 0
    fi

    echo "[INFO] Downloading $name..."
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$dest"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$dest" "$url"
    else
        echo "[ERROR] 'curl' or 'wget' is required." >&2
        return 1
    fi
}

install_dependency() {
    local name=$1
    local url=$2
    local check_file=$3
    local sub_path=${4:-}"" # Optional subpath within the zip
    local target_base=${5:-"$LIB_DIR"} # Optional target base directory

    # [SIL-2] Check if already installed
    if [ -f "$target_base/$name/$check_file" ] || \
       [ -f "$target_base/$name/src/$check_file" ] || \
       [ -f "$target_base/$name/etl/$check_file" ]; then
        echo "[INFO] $name already installed."
        return 0
    fi

    echo "[WARN] $name missing. Installing..."
    
    local tmp_dir
    tmp_dir=$(mktemp -d)
    local zip_path="$tmp_dir/$name.zip"

    if ! download_zip "$name" "$url" "$zip_path"; then
        echo "[ERROR] Failed to download $name." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    if ! command -v unzip >/dev/null 2>&1; then
        echo "[ERROR] 'unzip' is required." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    unzip -q "$zip_path" -d "$tmp_dir"
    
    local extracted_root
    extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d -name "$name-*" -o -name "$(echo "$name" | tr '[:upper:]' '[:lower:]')-*" | head -n1)
    
    if [ -z "$extracted_root" ]; then
        # Fallback for archives that don't follow the name-version pattern
        extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d ! -path "$tmp_dir" | head -n1)
    fi

    local source_path="$extracted_root"
    if [ -n "$sub_path" ]; then
        source_path="$extracted_root/$sub_path"
    fi

    mkdir -p "$target_base"
    rm -rf "$target_base/$name"
    cp -a "$source_path" "$target_base/$name"
    echo "[OK] $name installed."
    
    rm -rf "$tmp_dir"
}

# --- Main Installation ---

# 1. External Dependencies (Installed to Arduino libraries folder)
install_dependency "FastCRC" "https://codeload.github.com/FrankBoesing/FastCRC/zip/refs/heads/master" "FastCRC.h"
install_dependency "PacketSerial" "https://codeload.github.com/bakercp/PacketSerial/zip/refs/heads/master" "PacketSerial.h"
# Crypto: Using OperatorFoundation standalone fork (simpler install, PlatformIO support)
install_dependency "Crypto" "https://codeload.github.com/OperatorFoundation/Crypto/zip/refs/heads/master" "Crypto.h"

# 2. Bundled Dependencies
# ETL is installed in two locations:
#   1. Global Arduino libraries (standard practice)
#   2. Local src/ directory (required for host-based unit tests and SIL-2 isolation)
# [OPTIMIZATION] Download once, copy to both locations
install_etl_dual() {
    local url="https://codeload.github.com/ETLCPP/etl/zip/refs/heads/master"
    local check_file="array.h"
    local sub_path="include/etl"
    local target1="$LIB_DIR"
    local target2="${LIB_ROOT}/src"

    local needs_t1=false
    local needs_t2=false

    # Check which targets need installation
    if [ ! -f "$target1/etl/$check_file" ] && [ ! -f "$target1/etl/etl/$check_file" ]; then
        needs_t1=true
    else
        echo "[INFO] etl already installed at $target1."
    fi
    if [ ! -f "$target2/etl/$check_file" ] && [ ! -f "$target2/etl/etl/$check_file" ]; then
        needs_t2=true
    else
        echo "[INFO] etl already installed at $target2."
    fi

    if [ "$needs_t1" = false ] && [ "$needs_t2" = false ]; then
        return 0
    fi

    local tmp_dir
    tmp_dir=$(mktemp -d)
    local zip_path="$tmp_dir/etl.zip"

    if ! download_zip "etl" "$url" "$zip_path"; then
        echo "[ERROR] Failed to download etl." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    unzip -q "$zip_path" -d "$tmp_dir"
    local extracted_root
    extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d -name "etl-*" | head -n1)
    local source_path="$extracted_root/$sub_path"

    if [ "$needs_t1" = true ]; then
        mkdir -p "$target1"
        rm -rf "$target1/etl"
        cp -a "$source_path" "$target1/etl"
        echo "[OK] etl installed to $target1."
    fi
    if [ "$needs_t2" = true ]; then
        mkdir -p "$target2"
        rm -rf "$target2/etl"
        cp -a "$source_path" "$target2/etl"
        echo "[OK] etl installed to $target2."
    fi

    rm -rf "$tmp_dir"
}
install_etl_dual

# Verify our own src directory exists
if [ ! -d "${LIB_ROOT}/src" ]; then
    echo "[ERROR] Source directory not found: ${LIB_ROOT}/src" >&2
    exit 1
fi

LIB_DST="$LIB_DIR/McuBridge"
echo "[INFO] Installing McuBridge to $LIB_DST..."

rm -rf "$LIB_DST"
mkdir -p "$LIB_DST"

cp -a "${LIB_ROOT}/library.properties" "$LIB_DST/"
cp -a "${LIB_ROOT}/src" "$LIB_DST/"
if [ -d "${LIB_ROOT}/examples" ]; then
    cp -a "${LIB_ROOT}/examples" "$LIB_DST/"
fi

echo "================================================================================"
echo "[SUCCESS] McuBridge and dependencies installed successfully."
echo "================================================================================"
