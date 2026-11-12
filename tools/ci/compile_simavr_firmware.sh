#!/usr/bin/env bash
#
# Compile Arduino Bridge firmware for AVR-based simavr emulation.
# Delegate to unified compile_firmware.sh [Rule 19]
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH_PATH="${1:-${SCRIPT_DIR}/../../mcubridge-library-arduino/examples/BridgeControl/BridgeControl.ino}"
FQBN="${2:-arduino:avr:mega}"
OUTPUT_DIR="${3:-}"

exec "${SCRIPT_DIR}/compile_firmware.sh" "$SKETCH_PATH" "$FQBN" ${OUTPUT_DIR:+"$OUTPUT_DIR"}
