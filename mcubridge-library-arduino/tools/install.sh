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

        unzip -q "$zip_path" -d "$tmp_dir"
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

# 1. Official Dependencies (Library Manager)
# We no longer vendor ETL or wolfSSL files into src/. 
# Users should install these libraries via the Arduino Library Manager.
if [ "${1:-}" == "" ]; then
    echo "[INFO] 'Embedded Template Library' dependency should be installed via Arduino Library Manager."
    echo "[INFO] 'wolfSSL' dependency should be installed via Arduino Library Manager."
else
    # In CI/CD or when a target directory is provided, we install them.
    # ETL: We copy the whole repository to the library directory.
    install_dependency "Embedded_Template_Library" "https://codeload.github.com/ETLCPP/etl/zip/refs/tags/20.48.1" "include/etl/algorithm.h" "$LIB_DIR"
    install_dependency "wolfSSL" "https://codeload.github.com/wolfSSL/wolfssl/zip/refs/tags/v5.9.2-stable" "wolfssl/wolfcrypt/settings.h" "$LIB_DIR"
    install_dependency "PacketSerial" "https://codeload.github.com/isantolin/PacketSerial2/zip/refs/heads/master" "src/Codecs/COBSR.h" "$LIB_DIR"
fi

# Unity test framework (host tests only)
install_dependency "Unity" \
    "https://codeload.github.com/ThrowTheSwitch/Unity/zip/refs/tags/v2.6.1" \
    "unity.h" \
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
