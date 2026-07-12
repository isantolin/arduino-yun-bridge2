#!/bin/bash
#
# McuBridge Arduino library install script - Robust version [SIL-2]
# This script installs the McuBridge library and its dependencies into the 
# Arduino libraries directory.

set -e
set -u

# Always work relative to the script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="$(cd "${LIB_ROOT}/.." && pwd)"

echo "================================================================================"
echo " McuBridge Arduino Library Installer"
echo "================================================================================"

# --- Path Detection ---
get_arduino_lib_dir() {
    if [ "${1:-}" != "" ]; then
        echo "$1"
        return
    fi
    case "$(uname)" in
        Darwin) echo "$HOME/Documents/Arduino/libraries" ;;
        Linux)
            if [ -d "$HOME/Arduino/libraries" ]; then
                echo "$HOME/Arduino/libraries"
            else
                echo "$HOME/Arduino/libraries"
            fi
            ;;
        *) echo "$HOME/Arduino/libraries" ;;
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

    if [ -f "$dest" ]; then return 0; fi
    echo "[INFO] Downloading $name..."
    if command -v curl >/dev/null 2>&1; then
        local attempt
        for attempt in 1 2 3; do
            if curl -fsSL --retry 5 --retry-delay 2 --connect-timeout 20 --max-time 180 "$url" -o "$dest"; then
                return 0
            fi
            echo "[WARN] curl download failed for $name (attempt $attempt/3)." >&2
            sleep 1
        done
        for attempt in 1 2; do
            if curl --http1.1 -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 180 "$url" -o "$dest"; then
                return 0
            fi
            echo "[WARN] curl HTTP/1.1 fallback failed for $name (attempt $attempt/2)." >&2
            sleep 1
        done
        return 1
    elif command -v wget >/dev/null 2>&1; then
        wget --tries=5 --waitretry=2 --timeout=20 -qO "$dest" "$url"
    else
        echo "[ERROR] 'curl' or 'wget' is required." >&2
        return 1
    fi
}

install_dependency() {
    local name=$1
    local url=$2
    local check_file=$3
    local sub_path=${4:-}""
    local target_base=${5:-"$LIB_DIR"}

    if [ -f "$target_base/$name/$check_file" ] || \
       [ -f "$target_base/$name/src/$check_file" ] || \
       [ -f "$target_base/$name/etl/$check_file" ]; then
        echo "[INFO] $name already installed."
        return 0
    fi

    echo "[WARN] $name missing. Installing..."
    local tmp_dir
    tmp_dir=$(mktemp -d -p "$LIB_DIR")
    local zip_path="$tmp_dir/$name.zip"

    if ! download_zip "$name" "$url" "$zip_path"; then
        echo "[ERROR] Failed to download $name." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    unzip -q "$zip_path" -d "$tmp_dir"
    # Find the directory that contains the files (excluding the zip itself)
    local extracted_root
    extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d ! -path "$tmp_dir" | head -n1)
    
    if [ -z "$extracted_root" ]; then
        echo "[ERROR] Could not find extracted directory for $name." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    local source_path="$extracted_root"
    # if [ -n "$sub_path" ]; then
    #    source_path="$extracted_root/$sub_path"
    # fi

    mkdir -p "$target_base/$name"
    # Copy ALL contents of extracted_root to target_base/$name
    cp -a "$extracted_root/." "$target_base/$name/"
    
    echo "[OK] $name installed."
    rm -rf "$tmp_dir"
}

# 1. Official Dependencies (Library Manager)
# We no longer vendor ETL or wolfSSL files into src/. 
# Users should install these libraries via the Arduino Library Manager.
if [ "${1:-}" == "" ]; then
    echo "[INFO] 'Embedded Template Library' dependency should be installed via Arduino Library Manager."
    echo "[INFO] 'wolfSSL' dependency should be installed via Arduino Library Manager."
else
    # In CI/CD or when a target directory is provided, we install them.
    # ETL: We copy the whole repository to the library directory.
    install_dependency "Embedded_Template_Library" "https://codeload.github.com/ETLCPP/etl/zip/refs/tags/20.47.1" "include/etl/algorithm.h" "" "$LIB_DIR"
    install_dependency "wolfSSL" "https://codeload.github.com/wolfSSL/wolfssl/zip/refs/tags/v5.9.1-stable" "wolfssl/wolfcrypt/settings.h" "" "$LIB_DIR"
    install_dependency "PacketSerial" "https://codeload.github.com/isantolin/PacketSerial2/zip/refs/heads/master" "src/Codecs/COBSR.h" "" "$LIB_DIR"
fi

# Unity test framework (host tests only)
install_dependency "Unity" \
    "https://codeload.github.com/ThrowTheSwitch/Unity/zip/refs/tags/v2.6.1" \
    "unity.h" \
    "src" \
    "${LIB_ROOT}/tests"

# --- Nanopb Core C Files ---
# Since these are ignored by .gitignore, we download them dynamically if missing.
install_nanopb_core() {
    local target_dir="${LIB_ROOT}/src"
    local version="nanopb-0.4.9.1"
    local base_url="https://raw.githubusercontent.com/nanopb/nanopb/${version}"
    local files=(
        "pb.h"
        "pb_common.h"
        "pb_common.c"
        "pb_decode.h"
        "pb_decode.c"
        "pb_encode.h"
        "pb_encode.c"
    )

    mkdir -p "$target_dir"
    for f in "${files[@]}"; do
        local dest="$target_dir/$f"
        if [ ! -f "$dest" ]; then
            echo "[INFO] Downloading Nanopb core file: $f..."
            if ! download_zip "$f" "$base_url/$f" "$dest"; then
                echo "[ERROR] Failed to download $f from $base_url/$f" >&2
                return 1
            fi
        fi
    done
}

install_nanopb_core

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
