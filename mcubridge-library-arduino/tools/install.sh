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
            if [ -d "$HOME/Documents/Arduino/libraries" ]; then
                echo "$HOME/Documents/Arduino/libraries"
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
    tmp_dir=$(mktemp -d)
    local zip_path="$tmp_dir/$name.zip"

    if ! download_zip "$name" "$url" "$zip_path"; then
        echo "[ERROR] Failed to download $name." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    unzip -q "$zip_path" -d "$tmp_dir"
    local extracted_root
    extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d -name "$name-*" -o -name "$(echo "$name" | tr '[:upper:]' '[:lower:]')-*" | head -n1)
    
    if [ -z "$extracted_root" ]; then
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

# 1. Bundled Dependencies (ETL)
ETL_VERSION="20.46.2"
install_etl_dual() {
    local url="https://codeload.github.com/ETLCPP/etl/zip/refs/tags/${ETL_VERSION}"
    local check_file="array.h"
    local sub_path="include/etl"
    local target1="$LIB_DIR"
    local target2="${LIB_ROOT}/src"

    local needs_t1=false
    local needs_t2=false

    if [ ! -f "$target1/etl/$check_file" ] && [ ! -f "$target1/etl/etl/$check_file" ]; then needs_t1=true; fi
    if [ ! -f "$target2/etl/$check_file" ] && [ ! -f "$target2/etl/etl/$check_file" ]; then needs_t2=true; fi

    if [ "$needs_t1" = false ] && [ "$needs_t2" = false ]; then
        echo "[INFO] etl already installed."
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
        if [ -f "$target1/etl/type_list.h" ]; then sed -i 's/std::is_same/etl::is_same/g' "$target1/etl/type_list.h"; fi
        echo "[OK] etl installed to $target1."
    fi
    if [ "$needs_t2" = true ]; then
        mkdir -p "$target2"
        rm -rf "$target2/etl"
        cp -a "$source_path" "$target2/etl"
        if [ -f "$target2/etl/type_list.h" ]; then sed -i 's/std::is_same/etl::is_same/g' "$target2/etl/type_list.h"; fi
        echo "[OK] etl installed to $target2."
    fi

    rm -rf "$tmp_dir"
}
install_etl_dual

# 2. Vendored WolfSSL
WOLFSSL_VERSION="5.7.0-stable"
install_wolfssl_vendored() {
    local url="https://codeload.github.com/wolfSSL/wolfssl/zip/refs/tags/v${WOLFSSL_VERSION}"
    local check_file="wolfssl/wolfcrypt/sha256.h"
    local target="${LIB_ROOT}/src"

    if [ -f "$target/$check_file" ]; then
        echo "[INFO] wolfssl already vendored at $target."
        return 0
    fi

    echo "[WARN] wolfssl missing. Vendoring necessary files..."

    local tmp_dir
    tmp_dir=$(mktemp -d)
    local zip_path="$tmp_dir/wolfssl.zip"

    if ! download_zip "wolfssl" "$url" "$zip_path"; then
        echo "[ERROR] Failed to download wolfssl." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    unzip -q "$zip_path" -d "$tmp_dir"
    local extracted_root
    extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d -name "wolfssl-*" | head -n1)

    mkdir -p "$target/wolfssl"
    mkdir -p "$target/wolfcrypt/src"

    # Copiar cabeceras
    cp -a "$extracted_root/wolfssl/wolfcrypt" "$target/wolfssl/"
    cp "$extracted_root/wolfssl/version.h" "$target/wolfssl/" 2>/dev/null || true
    cp "$extracted_root/wolfssl/options.h" "$target/wolfssl/" 2>/dev/null || true

    # [HACK SIL-2] Generar user_settings.h dinámicamente y obligar a wolfSSL a usarlo.
    # Esto soluciona los problemas de "Unidades de Traducción Aisladas" en compiladores Arduino,
    # y evita la dependencia de archivos faltantes en el repositorio de CI.
    local settings_dir="$target/wolfssl/wolfcrypt"
    cat << 'EOF_WOLFSSL' > "$settings_dir/user_settings.h"
#ifndef WOLFSSL_USER_SETTINGS_H
#define WOLFSSL_USER_SETTINGS_H

/* * [WOLFSSL CONFIGURATION] 
 * Centralized settings for wolfCrypt without heap and optimized for AVR.
 */

/* Entorno Arduino Bare-Metal (Previene inclusión de <pthread.h>) */
#define WOLFSSL_ARDUINO
#define SINGLE_THREADED

/* [SIL-2] No dynamic memory allocation */
#define WOLFSSL_STATIC_MEMORY
#define WOLFSSL_NO_MALLOC
#define WOLFSSL_MALLOC_CHECK

/* [AVR] Optimization - DISABLED for Host Tests if not on AVR */
#if defined(ARDUINO_ARCH_AVR)
#define WOLFSSL_AVR
#define USE_SLOW_SHA256
/* #define WOLFSSL_SMALL_STACK // ERROR: Conflicto absoluto con WOLFSSL_NO_MALLOC */
#endif

/* [PROTOCOL] Required primitives only */
#define WOLFCRYPT_ONLY
#define NO_AES
#define NO_RSA
#define NO_DSA
#define NO_DH
#define NO_PWDBASED
#define NO_DES3
#define NO_MD5
#define NO_RC4
#define NO_ASN
#define NO_CODING
#define NO_FILESYSTEM
#define NO_SIG_WRAPPER
#define NO_OLD_TLS

/* [FEATURES] SHA-256, HMAC and HKDF */
#define WOLFSSL_SHA256
#define WOLFSSL_HMAC
#ifndef HAVE_HKDF
#define HAVE_HKDF
#endif
#ifndef WOLFSSL_HKDF
#define WOLFSSL_HKDF
#endif

/* Explicitly disable other hashes */
#define NO_SHA
#define NO_MD4
#define NO_MD2

/* [SECURITY] Hardening */
#define WOLFSSL_FORCE_ZERO
#define WOLFSSL_NO_FLOAT
#define NO_WRITEV
#define NO_MAIN_DRIVER

#endif /* WOLFSSL_USER_SETTINGS_H */
EOF_WOLFSSL

    local settings_file="$settings_dir/settings.h"
    if [ -f "$settings_file" ]; then
        echo "#define WOLFSSL_USER_SETTINGS 1" | cat - "$settings_file" > temp_settings.h
        mv temp_settings.h "$settings_file"
    fi

    # Copiar solo fuentes esenciales
    local required_c_files="sha256.c hmac.c hash.c error.c logging.c wc_port.c memory.c wc_encrypt.c"
    for f in $required_c_files; do
        if [ -f "$extracted_root/wolfcrypt/src/$f" ]; then
            cp "$extracted_root/wolfcrypt/src/$f" "$target/wolfcrypt/src/"
        fi
    done

    echo "[OK] wolfssl vendored to $target."
    rm -rf "$tmp_dir"
}
install_wolfssl_vendored

# 3. Nanopb
NANOPB_VERSION="0.4.9.1"
install_nanopb() {
    local url="https://codeload.github.com/nanopb/nanopb/zip/refs/tags/${NANOPB_VERSION}"
    local check_file="pb.h"
    local target="${LIB_ROOT}/src"

    if [ -f "$target/nanopb/$check_file" ]; then
        echo "[INFO] nanopb already installed at $target."
        return 0
    fi

    echo "[WARN] nanopb missing. Installing..."

    local tmp_dir
    tmp_dir=$(mktemp -d)
    local zip_path="$tmp_dir/nanopb.zip"

    if ! download_zip "nanopb" "$url" "$zip_path"; then
        echo "[ERROR] Failed to download nanopb." >&2
        rm -rf "$tmp_dir"
        return 1
    fi

    unzip -q "$zip_path" -d "$tmp_dir"
    local extracted_root
    extracted_root=$(find "$tmp_dir" -maxdepth 1 -type d -name "nanopb-*" | head -n1)

    local nanopb_files="pb.h pb_common.c pb_common.h pb_decode.c pb_decode.h pb_encode.c pb_encode.h"

    mkdir -p "$target/nanopb"
    for f in $nanopb_files; do
        cp "$extracted_root/$f" "$target/nanopb/"
    done
    echo "[OK] nanopb ${NANOPB_VERSION} installed to $target."

    rm -rf "$tmp_dir"
}
install_nanopb

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
