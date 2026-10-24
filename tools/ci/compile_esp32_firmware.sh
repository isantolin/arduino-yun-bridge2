#!/usr/bin/env bash
#
# Compile Arduino Bridge firmware for QEMU Xtensa emulation targeting Arduino Nano ESP32.
# Delegate to unified compile_firmware.sh [Rule 19]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH_PATH="${1:-${SCRIPT_DIR}/../../mcubridge-library-arduino/examples/BridgeControl/BridgeControl.ino}"
FQBN="${2:-arduino:esp32:nano_nora}"
OUTPUT_DIR="${3:-}"

exec "${SCRIPT_DIR}/compile_firmware.sh" "$SKETCH_PATH" "$FQBN" ${OUTPUT_DIR:+"$OUTPUT_DIR"}
