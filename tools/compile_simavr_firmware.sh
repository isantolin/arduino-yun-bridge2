#!/usr/bin/env bash
#
# Compile Arduino Bridge firmware for simavr emulation across supported AVR boards:
#   - arduino:avr:yun  (ATmega32u4 @ 16MHz)
#   - arduino:avr:uno  (ATmega328P @ 16MHz)
#   - arduino:avr:mega (ATmega2560 @ 16MHz)
#

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"

SKETCH_PATH="${1:-${LIB_DIR}/examples/BridgeControl/BridgeControl.ino}"
FQBN="${2:-arduino:avr:mega}"
OUTPUT_DIR="${3:-${ROOT_DIR}/build/simavr/${FQBN//:/-}}"

if ! command -v arduino-cli >/dev/null 2>&1; then
    echo "ERROR: arduino-cli is not installed or not in PATH." >&2
    exit 1
fi

USER_LIB_DIR="${HOME}/Arduino/libraries"
mkdir -p "$USER_LIB_DIR"

echo "[simavr-build] Installing required library dependencies..."
"${LIB_DIR}/tools/install.sh" "$USER_LIB_DIR"

# Patch official wolfSSL with our user_settings.h
for wolf_dir in "$USER_LIB_DIR/wolfSSL" "$USER_LIB_DIR/wolfssl" "${ROOT_DIR}/.dummy_libs/wolfSSL" "${ROOT_DIR}/.dummy_libs/wolfssl"; do
    if [ -d "$wolf_dir" ]; then
        echo "[simavr-build] Patching wolfSSL at $wolf_dir with user_settings.h..."
        mkdir -p "$wolf_dir/src" "$wolf_dir/wolfssl"
        cp "$LIB_DIR/src/user_settings.h" "$wolf_dir/user_settings.h"
        cp "$LIB_DIR/src/user_settings.h" "$wolf_dir/src/user_settings.h"
        cp "$LIB_DIR/src/user_settings.h" "$wolf_dir/wolfssl/user_settings.h"
        
        # Patch gmtime_r in wc_port.c
        for wcf in "$wolf_dir/src/wolfcrypt/src/wc_port.c" "$wolf_dir/wolfcrypt/src/wc_port.c"; do
            if [ -f "$wcf" ]; then
                sed -i 's/#if defined(WOLFSSL_GMTIME)/#if defined(WOLFSSL_GMTIME) \&\& !defined(HAVE_GMTIME_R)/' "$wcf" || true
            fi
        done
    fi
done

# Generate protocol stubs if missing
if [ ! -f "${LIB_DIR}/src/protocol/rpc_protocol.h" ]; then
    echo "[simavr-build] Generating protocol bindings..."
    python3 "${ROOT_DIR}/tools/protocol/generate.py" \
        --spec "${ROOT_DIR}/tools/protocol/mcubridge.proto" \
        --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
        --cpp "${LIB_DIR}/src/protocol/rpc_protocol.h" \
        --cpp-structs "${LIB_DIR}/src/protocol/rpc_structs.h" \
        --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"
fi

COMMON_FLAGS="-flto -fno-strict-aliasing -Wno-lto-type-mismatch -DWOLFSSL_USER_SETTINGS -DPB_BUFFER_ONLY=1 -DPB_NO_ERRMSG=1 -DSERIAL_RX_BUFFER_SIZE=256 -DSERIAL_TX_BUFFER_SIZE=256"
BUILD_FLAGS=(
    "--fqbn" "$FQBN"
    "--library" "$LIB_DIR"
    "--libraries" "$USER_LIB_DIR"
    "--warnings" "default"
    "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions $COMMON_FLAGS -DETL_NO_STL -I$USER_LIB_DIR/Embedded_Template_Library/include"
    "--build-property" "compiler.c.extra_flags=-std=gnu11 $COMMON_FLAGS -I$USER_LIB_DIR/Embedded_Template_Library/include"
    "--build-property" "compiler.c.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch"
    "--build-property" "compiler.cpp.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch"
    "--build-property" "compiler.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch"
    "--build-path" "$OUTPUT_DIR"
)

mkdir -p "$OUTPUT_DIR"
echo "[simavr-build] Compiling $SKETCH_PATH for $FQBN..."
arduino-cli compile --clean "${BUILD_FLAGS[@]}" "$SKETCH_PATH"

SKETCH_NAME="$(basename "$(dirname "$SKETCH_PATH")")"
ELF_FILE="${OUTPUT_DIR}/${SKETCH_NAME}.ino.elf"

if [ -f "$ELF_FILE" ]; then
    echo "[simavr-build] Successfully compiled: $ELF_FILE"
    cp "$ELF_FILE" "${OUTPUT_DIR}/firmware.elf"
    echo "[simavr-build] Output available at: ${OUTPUT_DIR}/firmware.elf"
else
    ANY_ELF="$(find "$OUTPUT_DIR" -maxdepth 1 -name "*.elf" | head -n 1)"
    if [ -n "$ANY_ELF" ]; then
        cp "$ANY_ELF" "${OUTPUT_DIR}/firmware.elf"
        echo "[simavr-build] Output available at: ${OUTPUT_DIR}/firmware.elf"
    else
        echo "ERROR: No ELF binary produced in $OUTPUT_DIR" >&2
        exit 1
    fi
fi
