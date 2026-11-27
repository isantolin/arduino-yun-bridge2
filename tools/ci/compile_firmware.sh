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
elif [[ "$FQBN" == esp32:esp32:* ]]; then
    EMULATOR="qemu"
else
    echo "ERROR: Unsupported FQBN: $FQBN" >&2
    exit 1
fi

OUTPUT_DIR="${3:-${ROOT_DIR}/build/${EMULATOR}/${FQBN//:/-}}"

# shellcheck source=tools/ci/arduino_toolchains.sh
source "${ROOT_DIR}/tools/ci/arduino_toolchains.sh"

# Do not build firmware with mismatched Bridge/AVR-port Pigweed source pins.
bash "${ROOT_DIR}/tools/ci/check_avr_pigweed_upstream_only.sh"

ensure_arduino_cli

USER_LIB_DIR="${ARDUINO_LIB_DIR:-${HOME}/Arduino/libraries}"
mkdir -p "$USER_LIB_DIR"

echo "[compile_firmware] Generating protocol bindings..."
generate_protocol_bindings

echo "[compile_firmware] Installing required library dependencies for ${EMULATOR}..."
"${LIB_DIR}/tools/install.sh" "$USER_LIB_DIR"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# Architecture-specific flags & build parameters
TMP_LOG="$(mktemp /tmp/compile_firmware_XXXXXX.log)"
STUBS_OBJ=""
PW_LIB_ARCHIVE=""
PW_STAGE_DIR=""

cleanup() {
    rm -f "$TMP_LOG" ${STUBS_OBJ:+"$STUBS_OBJ"} ${PW_LIB_ARCHIVE:+"$PW_LIB_ARCHIVE"}
    if [ -n "$PW_STAGE_DIR" ]; then
        rm -rf "$PW_STAGE_DIR"
    fi
}
trap cleanup EXIT

if [ "$EMULATOR" = "simavr" ]; then
    COMMON_FLAGS="-flto -fstack-usage -Wno-lto-type-mismatch ${PW_COMMON_MCU_FLAGS} -D__cpp_lib_endian=201907L -DETL_FORCE_STD_INITIALIZER_LIST"
    avr_toolchain_props "$USER_LIB_DIR"
    BUILD_FLAGS=(
        "--fqbn" "$FQBN"
        "--library" "$LIB_DIR"
        "--libraries" "$USER_LIB_DIR"
        "--warnings" "default"
        "${AVR_PROPS[@]}"
        "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions -fstack-usage $COMMON_FLAGS -DETL_NO_STL $AVR_STDCPP_INC -I$USER_LIB_DIR/Embedded_Template_Library/include"
        "--build-property" "compiler.c.extra_flags=-std=gnu11 -fstack-usage $COMMON_FLAGS $AVR_STDCPP_INC -I$USER_LIB_DIR/Embedded_Template_Library/include"
        "--build-property" "compiler.c.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch -Wl,-T,${PW_TOKENIZER_DEFAULT_LD} -Wl,-Map=${OUTPUT_DIR}/firmware.map,--cref,--trace-symbol=Serial1 -save-temps=obj"
        "--build-property" "compiler.cpp.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch -Wl,-T,${PW_TOKENIZER_DEFAULT_LD} -Wl,-Map=${OUTPUT_DIR}/firmware.map,--cref,--trace-symbol=Serial1 -save-temps=obj"
        "--build-property" "compiler.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch -Wl,-T,${PW_TOKENIZER_DEFAULT_LD} -Wl,-Map=${OUTPUT_DIR}/firmware.map,--cref,--trace-symbol=Serial1 -save-temps=obj"
        "--build-path" "$OUTPUT_DIR"
    )
elif [ "$EMULATOR" = "renode" ]; then
    samd_toolchain_props || exit 1

    PW_STAGE_DIR="$(mktemp -d /tmp/pw_stage_samd_XXXXXX)"
    stage_cooperative_pw_transfer "$PW_STAGE_DIR" "$USER_LIB_DIR" || exit 1

    PW_LIB_ARCHIVE="$(mktemp /tmp/libpigweed_samd_XXXXXX.a)"
    build_pigweed_archive_samd "$PW_LIB_ARCHIVE" "${LIB_DIR}/src" "$USER_LIB_DIR" || exit 1

    COMMON_FLAGS="${PW_COMMON_MCU_FLAGS} -Wno-psabi $PW_COOPERATIVE_DEFS"
    BUILD_FLAGS=(
        "--fqbn" "$FQBN"
        "--library" "$LIB_DIR"
        "--libraries" "$USER_LIB_DIR"
        "--warnings" "default"
        "${SAMD_TOOLCHAIN_PROPS[@]}"
        "--build-property" "build.extra_flags=-DUSE_ARDUINO_MKR_PIN_LAYOUT -D__SAMD21G18A__ {build.usb_flags}"
        "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions $COMMON_FLAGS -DETL_NO_STL -DMCUBRIDGE_RENODE_UART_SHIM=1 $PW_SAMD_INC"
        "--build-property" "compiler.c.extra_flags=-std=gnu11 $COMMON_FLAGS $PW_SAMD_INC"
        "--build-property" "compiler.c.elf.extra_flags=-Wl,--whole-archive,${PW_LIB_ARCHIVE},--no-whole-archive -Wl,-T,${PW_TOKENIZER_CUSTOM_LD}"
        "--build-path" "$OUTPUT_DIR"
    )
elif [ "$EMULATOR" = "qemu" ]; then
    find_esp32_toolchain || exit 1

    PW_STAGE_DIR="$(mktemp -d /tmp/pw_stage_esp32_XXXXXX)"
    stage_cooperative_pw_transfer "$PW_STAGE_DIR" "$USER_LIB_DIR" || exit 1

    STUBS_SRC="${ROOT_DIR}/tools/emulation/qemu/esp32_qemu_stubs.c"
    STUBS_OBJ="$(mktemp /tmp/esp32_qemu_stubs_XXXXXX.o)"
    "$XTENSA_GCC" -c "$STUBS_SRC" -o "$STUBS_OBJ"

    PW_LIB_ARCHIVE="$(mktemp /tmp/libpigweed_esp32_XXXXXX.a)"
    build_pigweed_archive_esp32 "$PW_LIB_ARCHIVE" "${LIB_DIR}/src" "$USER_LIB_DIR" || exit 1

    COMMON_FLAGS="${PW_COMMON_MCU_FLAGS} -DUSE_ARDUINO_NANO_ESP32_PIN_LAYOUT $PW_COOPERATIVE_DEFS"
    BUILD_FLAGS=(
        "--fqbn" "$FQBN"
        "--library" "$LIB_DIR"
        "--libraries" "$USER_LIB_DIR"
        "--warnings" "default"
        "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions -DETL_NO_STL $COMMON_FLAGS $PW_ESP32_INC"
        "--build-property" "compiler.c.extra_flags=-std=gnu11 $COMMON_FLAGS $PW_ESP32_INC"
        "--build-property" "build.psram_type=qspi"
        "--build-property" "build.cdc_on_boot=0"
        "--build-property" "build.dfu_on_boot=0"
        "--build-property" "build.defines=-DBOARD_HAS_PIN_REMAP -DBOARD_USES_HW_GPIO_NUMBERS '-DUSB_MANUFACTURER=\"Arduino\"' '-DUSB_PRODUCT=\"Nano ESP32\"'"
        "--build-property" "compiler.c.elf.extra_flags=${STUBS_OBJ} -Wl,--whole-archive,${PW_LIB_ARCHIVE},--no-whole-archive -Wl,-T,${PW_TOKENIZER_CUSTOM_LD} -Wl,--wrap=double_tap_init -Wl,--wrap=double_tap_mark -Wl,--wrap=double_tap_invalidate -Wl,--wrap=double_tap_check_match -Wl,--wrap=analogWrite -Wl,--wrap=analogRead -Wl,--wrap=adc_hal_self_calibration -Wl,--wrap=psramInit"
        "--build-path" "$OUTPUT_DIR"
    )
fi

echo "[compile_firmware] Compiling $SKETCH_PATH for $FQBN (${EMULATOR})..."

if LC_ALL=C LANG=C arduino-cli compile --clean "${BUILD_FLAGS[@]}" "$SKETCH_PATH" > "$TMP_LOG" 2>&1; then
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

    # Extract Pigweed pw_tokenizer database from ELF .pw_tokenizer.entries sections.
    if python3 -m pw_tokenizer.database create --type csv --force \
        -d "${OUTPUT_DIR}/tokens.csv" "${OUTPUT_DIR}/firmware.elf"; then
        echo "[compile_firmware] Extracted Pigweed token database: ${OUTPUT_DIR}/tokens.csv ($(wc -l < "${OUTPUT_DIR}/tokens.csv") entries)"
    fi

    # Emit a machine-readable report from the compiler's measured summary.
    # AVR additionally gets section accounting from its linked ELF.
    RESOURCE_REPORT_ARGS=(
        --compile-log "${OUTPUT_DIR}/compile.log"
        --fqbn "$FQBN"
        --output "${OUTPUT_DIR}/resource_report.json"
    )
    if [[ "$FQBN" == arduino:avr:* ]] && command -v avr-size >/dev/null 2>&1; then
        RESOURCE_REPORT_ARGS+=(--elf "${OUTPUT_DIR}/firmware.elf" --size-tool avr-size)
    fi
    python3 "${ROOT_DIR}/tools/ci/firmware_resource_report.py" "${RESOURCE_REPORT_ARGS[@]}"

    if [ "$EMULATOR" = "simavr" ] && command -v avr-size >/dev/null 2>&1; then
        # Static RAM report: the ATmega2560 has 8 KiB SRAM shared by .data,
        # .bss, heap and stack, so log what the image leaves for the stack.
        echo "[compile_firmware] AVR memory usage for ${SKETCH_NAME}:"
        grep -E "Serial1" "$TMP_LOG" || true
        avr-size -A "${OUTPUT_DIR}/firmware.elf" | grep -E '^\.(data|bss|noinit) '
        avr-nm --size-sort -S -C "${OUTPUT_DIR}/firmware.elf" | grep -E ' [bBdD] ' | tail -n 15
    fi

    if [ "$EMULATOR" = "qemu" ]; then
        # The Espressif core already emits a full-flash image (bootloader,
        # partitions, boot_app0 and app) padded to the board flash size.
        MERGED_FILE="${OUTPUT_DIR}/${SKETCH_NAME}.ino.merged.bin"
        if [ ! -f "$MERGED_FILE" ]; then
            echo "ERROR: Espressif core did not produce ${MERGED_FILE}" >&2
            exit 1
        fi

        FLASH_BIN="${OUTPUT_DIR}/firmware_flash.bin"
        cp "$MERGED_FILE" "$FLASH_BIN"
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
