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
    
    # Flatten only headers for Arduino compatibility (#include <mpack.h>)
    # Do NOT copy .c sources: arduino-cli would compile both root and src/mpack/, causing duplicate symbols.
    if [ "$name" == "mpack" ]; then
        echo "[INFO] Flattening mpack library for Arduino compatibility..."
        mkdir -p "$target_base/$name/src"
        # Move all .h and .c files from src/mpack/ to src/
        find "$target_base/$name/src/mpack/" -maxdepth 1 -name "*.h" -exec cp -a {} "$target_base/$name/src/" \;
        find "$target_base/$name/src/mpack/" -maxdepth 1 -name "*.c" -exec cp -a {} "$target_base/$name/src/" \;
        # Remove the now redundant subdirectory to avoid duplicate symbols during recursive compilation
        rm -rf "$target_base/$name/src/mpack"

        # [HOT-PATCH] Fix MPack C++ overloads for AVR (missing double support)
        echo "[INFO] Patching MPack for AVR C++ compatibility..."
        local f="$target_base/$name/src/mpack-writer.h"
        if [ -f "$f" ]; then
            python3 -c "
import sys
path = '$f'
with open(path, 'r') as f:
    content = f.read()
f_old = 'MPACK_INLINE void mpack_write(mpack_writer_t* writer, float value) {\n    mpack_write_float(writer, value);\n}'
f_new = '#if MPACK_FLOAT\n' + f_old + '\n#endif'
if f_old in content and '#if MPACK_FLOAT' not in content:
    content = content.replace(f_old, f_new)
d_old = 'MPACK_INLINE void mpack_write(mpack_writer_t* writer, double value) {\n    mpack_write_double(writer, value);\n}'
d_new = '#if MPACK_DOUBLE\n' + d_old + '\n#endif'
if d_old in content and '#if MPACK_DOUBLE' not in content:
    content = content.replace(d_old, d_new)
fkv_old = 'MPACK_INLINE void mpack_write_kv(mpack_writer_t* writer, const char *key, float value) {\n    mpack_write_cstr(writer, key);\n    mpack_write_float(writer, value);\n}'
fkv_new = '#if MPACK_FLOAT\n' + fkv_old + '\n#endif'
if fkv_old in content and '#if MPACK_FLOAT' not in content:
    content = content.replace(fkv_old, fkv_new)
dkv_old = 'MPACK_INLINE void mpack_write_kv(mpack_writer_t* writer, const char *key, double value) {\n    mpack_write_cstr(writer, key);\n    mpack_write_double(writer, value);\n}'
dkv_new = '#if MPACK_DOUBLE\n' + dkv_old + '\n#endif'
if dkv_old in content and '#if MPACK_DOUBLE' not in content:
    content = content.replace(dkv_old, dkv_new)
with open(path, 'w') as f:
    f.write(content)
"
        fi
    fi
    
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
    install_dependency "PacketSerial" "https://codeload.github.com/isantolin/PacketSerial2/zip/refs/heads/master" "PacketSerial.h" "" "$LIB_DIR"
    install_dependency "mpack" "https://github.com/ludocode/mpack/archive/refs/heads/develop.zip" "src/mpack/mpack.h" "" "$LIB_DIR"
    install_dependency "ArduinoJson" "https://codeload.github.com/bblanchon/ArduinoJson/zip/refs/tags/v7.4.3" "ArduinoJson.h" "" "$LIB_DIR"
    
    # Ensure mpack has a library.properties for arduino-cli recognition
    if [ ! -f "$LIB_DIR/mpack/library.properties" ]; then
        echo "[INFO] Creating library.properties for mpack..."
        cat > "$LIB_DIR/mpack/library.properties" <<EOF
name=mpack
version=1.1.0
author=Nicholas Fraser
maintainer=Nicholas Fraser
sentence=A high-performance MessagePack encoder/decoder.
paragraph=MPack is a high-performance encoder and decoder for the MessagePack serialization format.
category=Data Storage
url=https://github.com/ludocode/mpack
architectures=*
includes=mpack.h
EOF
    fi
fi

# Unity test framework (host tests only)
install_dependency "Unity" \
    "https://codeload.github.com/ThrowTheSwitch/Unity/zip/refs/tags/v2.6.1" \
    "unity.h" \
    "src" \
    "${LIB_ROOT}/tests"

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
