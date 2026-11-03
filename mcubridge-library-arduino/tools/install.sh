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
        local h3_flag=""
        if curl --version 2>&1 | grep -qi "HTTP3"; then
            h3_flag="--http3"
        fi
        curl $h3_flag -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 180 "$url" -o "$dest"
        return $?
    elif command -v wget >/dev/null 2>&1; then
        wget --tries=5 --waitretry=2 --timeout=20 -qO "$dest" "$url"
    else
        echo "[ERROR] 'curl' or 'wget' is required." >&2
        return 1
    fi
}

# Marker file written only after a dependency has been fully staged and
# atomically moved into place. Its presence (with matching URL) is the sole
# source of truth for "already installed" — a single header/source file is
# not sufficient evidence, since a partially copied tree (e.g. a run
# interrupted by a CI timeout, disk pressure, or a concurrent invocation of
# this script against the same target directory) can still contain that one
# file while missing hundreds of others.
INSTALL_MARKER=".mcubridge_install_complete"

install_dependency() {
    local name=$1
    local url=$2
    local check_file=$3
    local target_base=${4:-"$LIB_DIR"}
    local expected_sha256=${5:-}

    mkdir -p "$target_base"
    local lock_file="$target_base/.${name}.install.lock"

    # Serialize concurrent invocations of this script against the same
    # target_base (e.g. parallel tox environments or agents sharing a
    # checkout). Without this lock, two processes can interleave
    # rm -rf/cp -a on the same destination and leave a corrupted,
    # partially-populated directory that the next run would otherwise
    # trust as "installed".
    (
        flock -x 200

        if [ -f "$target_base/$name/$INSTALL_MARKER" ] && \
           grep -qxF "$url" "$target_base/$name/$INSTALL_MARKER" && \
           { [ -f "$target_base/$name/$check_file" ] || \
             [ -f "$target_base/$name/src/$check_file" ] || \
             [ -f "$target_base/$name/etl/$check_file" ]; }; then
            echo "[INFO] $name already installed."
            exit 0
        fi

        echo "[WARN] $name missing or incomplete. Installing..."
        local tmp_dir
        tmp_dir=$(mktemp -d -p "$target_base")
        local zip_path="$tmp_dir/$name.zip"

        if ! download_zip "$name" "$url" "$zip_path"; then
            echo "[ERROR] Failed to download $name." >&2
            rm -rf "$tmp_dir"
            exit 1
        fi

        if [ -n "$expected_sha256" ]; then
            if ! printf '%s  %s\n' "$expected_sha256" "$zip_path" | sha256sum --check --status; then
                echo "[ERROR] SHA-256 verification failed for $name." >&2
                rm -rf "$tmp_dir"
                exit 1
            fi
        fi

        case "$url" in
            *.tar.bz2) tar -xjf "$zip_path" -C "$tmp_dir" ;;
            *.tar.gz) tar -xzf "$zip_path" -C "$tmp_dir" ;;
            *) unzip -q "$zip_path" -d "$tmp_dir" ;;
        esac
        # Find the directory that contains the files (excluding the zip itself)
        local extracted_root
        extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d ! -path "$tmp_dir" | head -n1)

        if [ -z "$extracted_root" ]; then
            echo "[ERROR] Could not find extracted directory for $name." >&2
            rm -rf "$tmp_dir"
            exit 1
        fi

        # Stage the full copy in a private, not-yet-visible directory. Only
        # once staging is fully complete do we write the completion marker
        # and atomically rename it into its final place (mv is atomic within
        # the same filesystem). This guarantees "$target_base/$name" is
        # never observable in a partially-copied state: it is either the
        # previous complete install, absent, or the new complete install.
        local staged_dir="$tmp_dir/staged-$name"
        mkdir -p "$staged_dir"
        cp -a "$extracted_root/." "$staged_dir/"
        printf '%s\n' "$url" > "$staged_dir/$INSTALL_MARKER"

        rm -rf "$target_base/$name"
        mv "$staged_dir" "$target_base/$name"

        echo "[OK] $name installed."
        rm -rf "$tmp_dir"
    ) 200>"$lock_file"
}

# --- [AUTO-GENERATED C++ DEPENDENCIES BEGIN] ---
ETL_VERSION="20.49.0"
UNITY_VERSION="v2.7.0"
NANOPB_VERSION="0.4.9.2"
NANOPB_CORE_FILES=("pb.h" "pb_common.h" "pb_common.c" "pb_decode.h" "pb_decode.c" "pb_encode.h" "pb_encode.c")
MBEDTLS_VERSION="mbedtls-4.2.0"
MBEDTLS_SHA256="2bed9d713b4668f76553b097e72b8aa30bc8f112a940d7ae228d524bbde6ffea"
# --- [AUTO-GENERATED C++ DEPENDENCIES END] ---

# 1. Official Dependencies (Library Manager)
# Install and patch dependencies in LIB_DIR directly.
rm -rf "$LIB_DIR/Embedded_Template_Library_ETL"
install_dependency "Embedded_Template_Library" "https://codeload.github.com/ETLCPP/etl/zip/refs/tags/${ETL_VERSION}" "include/etl/algorithm.h" "$LIB_DIR"
if [ -d "$LIB_DIR/Embedded_Template_Library" ]; then
    mkdir -p "$LIB_DIR/Embedded_Template_Library/src"
    cp -ru "$LIB_DIR/Embedded_Template_Library/include/"* "$LIB_DIR/Embedded_Template_Library/src/" 2>/dev/null || true
    cp -ru "$LIB_DIR/Embedded_Template_Library/include/"* "$LIB_DIR/Embedded_Template_Library/" 2>/dev/null || true
    if [ -f "$LIB_DIR/Embedded_Template_Library/arduino/Embedded_Template_Library.h" ]; then
        cp "$LIB_DIR/Embedded_Template_Library/arduino/Embedded_Template_Library.h" "$LIB_DIR/Embedded_Template_Library/src/Embedded_Template_Library.h"
        cp "$LIB_DIR/Embedded_Template_Library/arduino/Embedded_Template_Library.h" "$LIB_DIR/Embedded_Template_Library/Embedded_Template_Library.h"
    fi
    if ! grep -q "includes=" "$LIB_DIR/Embedded_Template_Library/library.properties" 2>/dev/null; then
        echo "includes=Embedded_Template_Library.h,etl/algorithm.h" >> "$LIB_DIR/Embedded_Template_Library/library.properties"
    fi
fi

# Mbed TLS is staged as an Arduino library with only the required modules.
MBEDTLS_URL="https://github.com/Mbed-TLS/mbedtls/releases/download/${MBEDTLS_VERSION}/${MBEDTLS_VERSION}.tar.bz2"
install_dependency "MbedTLS" "$MBEDTLS_URL" "tf-psa-crypto/include/psa/crypto.h" "$LIB_DIR" "$MBEDTLS_SHA256"
MBEDTLS_LIB="$LIB_DIR/MbedTLS"
MBEDTLS_SRC="$MBEDTLS_LIB/src"
MBEDTLS_CT_SOURCE="$MBEDTLS_LIB/tf-psa-crypto/utilities/constant_time.c"
if ! grep -Fq "AVR constant-time comparison requires byte-wise access" "$MBEDTLS_CT_SOURCE"; then
    patch --batch --forward --fuzz=0 -p1 -d "$MBEDTLS_LIB/tf-psa-crypto" \
        < "$LIB_ROOT/patches/mbedtls-4.2.0-avr-ct-memcmp.patch"
fi
rm -rf "$MBEDTLS_SRC"
mkdir -p "$MBEDTLS_SRC"
cp -a "$MBEDTLS_LIB/include/mbedtls" "$MBEDTLS_SRC/"
cp -a "$MBEDTLS_LIB/tf-psa-crypto/include/." "$MBEDTLS_SRC/"
mkdir -p "$MBEDTLS_SRC/tf-psa-crypto/drivers"
for component in core extras platform utilities; do
    cp -a "$MBEDTLS_LIB/tf-psa-crypto/$component" "$MBEDTLS_SRC/tf-psa-crypto/"
done
cp -a "$MBEDTLS_LIB/tf-psa-crypto/drivers/builtin" "$MBEDTLS_SRC/tf-psa-crypto/drivers/"
cp -a "$MBEDTLS_LIB/tf-psa-crypto/drivers/builtin/include/." "$MBEDTLS_SRC/"
for component in core dispatch extras platform utilities; do
    find "$MBEDTLS_LIB/tf-psa-crypto/$component" -maxdepth 1 -type f -name "*.h" \
        -exec cp -a {} "$MBEDTLS_SRC/" \;
done
find "$MBEDTLS_LIB/tf-psa-crypto/drivers/builtin/src" -maxdepth 1 -type f -name "*.h" \
    -exec cp -a {} "$MBEDTLS_SRC/" \;
cp "$LIB_ROOT/src/config/mbedtls_config.h" "$MBEDTLS_SRC/mbedtls/mbedtls_config.h"
cp "$LIB_ROOT/src/config/psa_crypto_config.h" "$MBEDTLS_SRC/psa/crypto_config.h"
cat > "$MBEDTLS_SRC/MbedTLS.h" <<'EOF'
#ifndef MCUBRIDGE_MBEDTLS_ARDUINO_H
#define MCUBRIDGE_MBEDTLS_ARDUINO_H

#include <psa/crypto.h>

#endif
EOF
cp "$MBEDTLS_SRC/MbedTLS.h" "$MBEDTLS_LIB/MbedTLS.h"
cat > "$MBEDTLS_LIB/library.properties" <<EOF
name=MbedTLS
version=${MBEDTLS_VERSION#mbedtls-}
author=Mbed TLS Contributors
maintainer=Mbed TLS Contributors
sentence=Minimal Mbed TLS cryptographic primitives used by McuBridge.
paragraph=Staged by the McuBridge installer from the pinned upstream release.
category=Communication
architectures=*
includes=MbedTLS.h,psa/crypto.h,psa/crypto_extra.h,mbedtls/platform_util.h
EOF

# Pigweed AVR library (Google Pigweed RPC, HDLC, Tokenizer, Chrono, Crypto for Arduino)
if [ -d "${ROOT_DIR}/third_party/avr_pigweed_port" ] || [ -f "${ROOT_DIR}/.gitmodules" ]; then
    if [ ! -f "${ROOT_DIR}/third_party/avr_pigweed_port/tools/export_arduino_library.py" ] && [ -d "${ROOT_DIR}/.git" ]; then
        git -C "${ROOT_DIR}" submodule update --init --recursive third_party/avr_pigweed_port 2>/dev/null || true
    fi
    if [ -f "${ROOT_DIR}/third_party/avr_pigweed_port/tools/export_arduino_library.py" ]; then
        python3 "${ROOT_DIR}/third_party/avr_pigweed_port/tools/export_arduino_library.py" "${ROOT_DIR}/third_party/avr_pigweed_port/dist/PigweedAVR"
        if [ -d "${ROOT_DIR}/third_party/avr_pigweed_port/dist/PigweedAVR" ]; then
            mkdir -p "$LIB_DIR"
            rm -rf "$LIB_DIR/PigweedAVR"
            cp -a "${ROOT_DIR}/third_party/avr_pigweed_port/dist/PigweedAVR" "$LIB_DIR/PigweedAVR"
        fi
    fi
fi

# Unity test framework (host tests only)
install_dependency "Unity" \
    "https://codeload.github.com/ThrowTheSwitch/Unity/zip/refs/tags/${UNITY_VERSION}" \
    "unity.h" \
    "${LIB_ROOT}/tests"

# --- Nanopb Core C Files ---
# Since these are ignored by .gitignore, we download them dynamically if missing.
install_nanopb_core() {
    local target_dir="${LIB_ROOT}/src"
    local version="nanopb-${NANOPB_VERSION}"
    local base_url="https://raw.githubusercontent.com/nanopb/nanopb/${version}"
    local files=("${NANOPB_CORE_FILES[@]}")

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
