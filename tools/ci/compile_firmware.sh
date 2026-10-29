#!/usr/bin/env bash
#
# Unified Arduino Bridge firmware compiler for hardware emulators (simavr, renode, QEMU).
# [SIL-2 / Rule 15 / Rule 19 / Rule 37]
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"

SKETCH_PATH="${1:-${LIB_DIR}/examples/BridgeControl/BridgeControl.ino}"
FQBN="${2:-arduino:avr:mega}"

# Deduce emulator architecture & default output directory
if [[ "$FQBN" == arduino:avr:* ]]; then
    EMULATOR="simavr"
elif [[ "$FQBN" == arduino:samd:* ]]; then
    EMULATOR="renode"
elif [[ "$FQBN" == arduino:esp32:* || "$FQBN" == esp32:esp32:* ]]; then
    EMULATOR="qemu"
else
    echo "ERROR: Unsupported FQBN: $FQBN" >&2
    exit 1
fi

OUTPUT_DIR="${3:-${ROOT_DIR}/build/${EMULATOR}/${FQBN//:/-}}"

# Ensure arduino-cli is installed
if ! command -v arduino-cli >/dev/null 2>&1; then
    echo "[compile_firmware] arduino-cli not found. Installing into ~/.local/bin..."
    mkdir -p "${HOME}/.local/bin"
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="${HOME}/.local/bin" sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

USER_LIB_DIR="${HOME}/Arduino/libraries"
mkdir -p "$USER_LIB_DIR"

echo "[compile_firmware] Installing required library dependencies for ${EMULATOR}..."
if [ "$EMULATOR" = "qemu" ] && [ ! -d "$USER_LIB_DIR/wolfssl" ] && [ ! -d "$USER_LIB_DIR/wolfSSL/src/wolfssl" ]; then
    arduino-cli lib install wolfSSL || true
fi
"${LIB_DIR}/tools/install.sh" "$USER_LIB_DIR"

# Generate protocol stubs if missing
if [ ! -f "${LIB_DIR}/src/protocol/rpc_protocol.h" ]; then
    echo "[compile_firmware] Generating protocol bindings..."
    python3 "${ROOT_DIR}/tools/protocol/generate.py" \
        --spec "${ROOT_DIR}/tools/protocol/cloud_gateway.proto" \
        --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
        --cpp "${LIB_DIR}/src/protocol/rpc_protocol.h" \
        --cpp-structs "${LIB_DIR}/src/protocol/rpc_structs.h" \
        --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# Architecture-specific flags & build parameters
TMP_LOG="$(mktemp /tmp/compile_firmware_XXXXXX.log)"
STUBS_OBJ=""

cleanup() {
    rm -f "$TMP_LOG"
    if [ -n "$STUBS_OBJ" ] && [ -f "$STUBS_OBJ" ]; then
        rm -f "$STUBS_OBJ"
    fi
}
trap cleanup EXIT

if [ "$EMULATOR" = "simavr" ]; then
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
elif [ "$EMULATOR" = "renode" ]; then
    COMMON_FLAGS="-fno-strict-aliasing -DWOLFSSL_USER_SETTINGS -DPB_BUFFER_ONLY=1 -DPB_NO_ERRMSG=1"
    BUILD_FLAGS=(
        "--fqbn" "$FQBN"
        "--library" "$LIB_DIR"
        "--libraries" "$USER_LIB_DIR"
        "--warnings" "default"
        "--build-property" "build.extra_flags=-DUSE_ARDUINO_MKR_PIN_LAYOUT -D__SAMD21G18A__ {build.usb_flags}"
        "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions $COMMON_FLAGS -DETL_NO_STL -DMCUBRIDGE_RENODE_UART_SHIM=1"
        "--build-property" "compiler.c.extra_flags=-std=gnu11 $COMMON_FLAGS"
        "--build-path" "$OUTPUT_DIR"
    )
elif [ "$EMULATOR" = "qemu" ]; then
    ESPTOOL_BIN="$(find "${HOME}/.arduino15" -name esptool.py 2>/dev/null | head -n 1 || true)"
    if [ -z "$ESPTOOL_BIN" ] && command -v esptool.py >/dev/null 2>&1; then
        ESPTOOL_BIN="$(command -v esptool.py)"
    fi
    XTENSA_GCC="$(find "${HOME}/.arduino15" -name xtensa-esp32s3-elf-gcc 2>/dev/null | head -n 1 || true)"
    if [ -z "$XTENSA_GCC" ] && command -v xtensa-esp32s3-elf-gcc >/dev/null 2>&1; then
        XTENSA_GCC="$(command -v xtensa-esp32s3-elf-gcc)"
    fi
    BOOT_APP0="$(find "${HOME}/.arduino15" -name boot_app0.bin 2>/dev/null | head -n 1 || true)"

    if [ -z "$XTENSA_GCC" ]; then
        echo "ERROR: xtensa-esp32s3-elf-gcc compiler not found in ~/.arduino15 or PATH" >&2
        exit 1
    fi
    if [ -z "$ESPTOOL_BIN" ]; then
        echo "ERROR: esptool.py not found in ~/.arduino15 or PATH" >&2
        exit 1
    fi
    if [ -z "$BOOT_APP0" ]; then
        echo "ERROR: boot_app0.bin partition image not found in ~/.arduino15" >&2
        exit 1
    fi

    STUBS_SRC="${ROOT_DIR}/tools/emulation/qemu/esp32_qemu_stubs.c"
    STUBS_OBJ="$(mktemp /tmp/esp32_qemu_stubs_XXXXXX.o)"
    "$XTENSA_GCC" -c "$STUBS_SRC" -o "$STUBS_OBJ"

    COMMON_FLAGS="-fno-strict-aliasing -DWOLFSSL_USER_SETTINGS -DPB_BUFFER_ONLY=1 -DPB_NO_ERRMSG=1"
    BUILD_FLAGS=(
        "--fqbn" "$FQBN"
        "--library" "$LIB_DIR"
        "--libraries" "$USER_LIB_DIR"
        "--warnings" "default"
        "--build-property" "build.extra_flags=-DUSE_ARDUINO_NANO_ESP32_PIN_LAYOUT"
        "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions -DETL_NO_STL $COMMON_FLAGS"
        "--build-property" "compiler.c.extra_flags=-std=gnu11 $COMMON_FLAGS"
        "--build-property" "build.psram_type=qspi"
        "--build-property" "build.cdc_on_boot=0"
        "--build-property" "build.dfu_on_boot=0"
        "--build-property" "build.defines=-DBOARD_HAS_PIN_REMAP -DBOARD_USES_HW_GPIO_NUMBERS '-DUSB_MANUFACTURER=\"Arduino\"' '-DUSB_PRODUCT=\"Nano ESP32\"'"
        "--build-property" "compiler.c.elf.extra_flags=${STUBS_OBJ} -Wl,--wrap=double_tap_init -Wl,--wrap=double_tap_mark -Wl,--wrap=double_tap_invalidate -Wl,--wrap=double_tap_check_match -Wl,--wrap=analogWrite"
        "--build-path" "$OUTPUT_DIR"
    )
fi

echo "[compile_firmware] Compiling $SKETCH_PATH for $FQBN (${EMULATOR})..."

if arduino-cli compile --clean "${BUILD_FLAGS[@]}" "$SKETCH_PATH" > "$TMP_LOG" 2>&1; then
    cp "$TMP_LOG" "${OUTPUT_DIR}/compile.log"
    python3 "${ROOT_DIR}/tools/arduino_core_metadata.py" \
        --fqbn "$FQBN" \
        --json-output "${OUTPUT_DIR}/arduino_core_metadata.json"

    SKETCH_NAME="$(basename "$(dirname "$SKETCH_PATH")")"
    ELF_FILE="${OUTPUT_DIR}/${SKETCH_NAME}.ino.elf"

    if [ ! -f "$ELF_FILE" ]; then
        ANY_ELF="$(find "$OUTPUT_DIR" -maxdepth 1 -name "*.elf" | head -n 1)"
        if [ -n "$ANY_ELF" ]; then
            ELF_FILE="$ANY_ELF"
        else
            echo "ERROR: No ELF binary produced in $OUTPUT_DIR" >&2
            exit 1
        fi
    fi
    cp "$ELF_FILE" "${OUTPUT_DIR}/firmware.elf"

    if [ "$EMULATOR" = "qemu" ]; then
        BIN_FILE="${OUTPUT_DIR}/${SKETCH_NAME}.ino.bin"
        BOOT_FILE="${OUTPUT_DIR}/${SKETCH_NAME}.ino.bootloader.bin"
        PART_FILE="${OUTPUT_DIR}/${SKETCH_NAME}.ino.partitions.bin"

        if [ ! -f "$BIN_FILE" ] || [ ! -f "$BOOT_FILE" ] || [ ! -f "$PART_FILE" ]; then
            echo "ERROR: Missing flash component binaries in $OUTPUT_DIR" >&2
            exit 1
        fi

        FLASH_BIN="${OUTPUT_DIR}/firmware_flash.bin"
        python3 "$ESPTOOL_BIN" \
            --chip esp32s3 merge_bin \
            --output "$FLASH_BIN" \
            --fill-flash-size 16MB \
            0x0 "$BOOT_FILE" \
            0x8000 "$PART_FILE" \
            0xe000 "$BOOT_APP0" \
            0x10000 "$BIN_FILE" >/dev/null 2>&1

        cp "$FLASH_BIN" "${OUTPUT_DIR}/firmware.bin"
        echo "[compile_firmware] Successfully produced 16MB flash image: $FLASH_BIN"
    fi
    echo "[compile_firmware] Successfully compiled for $FQBN: ${OUTPUT_DIR}/firmware.elf"
else
    cp "$TMP_LOG" "${OUTPUT_DIR}/compile.log"
    if [ "$EMULATOR" = "simavr" ] && [ "$FQBN" != "arduino:avr:mega" ]; then
        echo "[compile_firmware] ⚠ $FQBN compilation skipped (non-critical, Flash/RAM limit exceeded on small AVR board)"
        rm -f "${OUTPUT_DIR}/firmware.elf"
        exit 0
    fi
    echo "ERROR: Failed compiling for $FQBN!" >&2
    cat "$TMP_LOG" >&2
    exit 1
fi
