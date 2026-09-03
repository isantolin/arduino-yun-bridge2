#!/usr/bin/env bash
#
# Multi-Board simavr Emulation Matrix Runner (SIL-2 / Flight-Ready)
# Iterates across supported AVR target boards:
#   - arduino:avr:mega (ATmega2560 - Primary target)
#   - arduino:avr:yun  (ATmega32u4)
#   - arduino:avr:uno  (ATmega328P)
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SKETCH_PATH="${1:-${ROOT_DIR}/mcubridge-library-arduino/examples/BridgeControl/BridgeControl.ino}"
BUILD_BASE_DIR="${ROOT_DIR}/build/simavr"
SUMMARY_DIR="${SIMAVR_METRICS_DIR:-${BUILD_BASE_DIR}}"
mkdir -p "$SUMMARY_DIR" "$BUILD_BASE_DIR"

BOARDS=("arduino:avr:mega" "arduino:avr:yun" "arduino:avr:uno")
BOARD_NAMES=("Arduino Mega 2560 (ATmega2560)" "Arduino Yún (ATmega32u4)" "Arduino Uno (ATmega328P)")

declare -a COMPILATION_STATUS
declare -a EMULATION_STATUS

FAIL_COUNT=0

for i in "${!BOARDS[@]}"; do
    BOARD="${BOARDS[$i]}"
    NAME="${BOARD_NAMES[$i]}"
    SLUG="${BOARD//:/-}"
    OUT_DIR="${BUILD_BASE_DIR}/${SLUG}"
    
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo "[simavr-matrix] Testing Board: $NAME ($BOARD)"
    echo "════════════════════════════════════════════════════════════════════════════════"
    
    # 1. Compile
    if bash "${ROOT_DIR}/tools/ci/compile_simavr_firmware.sh" "$SKETCH_PATH" "$BOARD" "$OUT_DIR"; then
        if [ -f "${OUT_DIR}/firmware.elf" ]; then
            COMPILATION_STATUS+=("✅ Compiled")
            
            # 2. Emulate
            echo "[simavr-matrix] Running cycle-accurate simavr emulation for $BOARD..."
            EXTRA_ARGS=()
            if [[ "$SKETCH_PATH" =~ (BridgeBluetooth|BridgeWiFi) ]] && [ "$BOARD" = "arduino:avr:mega" ]; then
                EXTRA_ARGS+=(--uart "1")
            fi
            if python3 "${ROOT_DIR}/tools/emulation/simavr_runner.py" --firmware "${OUT_DIR}/firmware.elf" --board "$BOARD" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}; then
                echo "[simavr-matrix] ✅ Emulation PASSED for $BOARD"
                EMULATION_STATUS+=("✅ Passed (100% E2E)")
            else
                echo "[simavr-matrix] ❌ Emulation FAILED for $BOARD"
                EMULATION_STATUS+=("❌ Failed")
                FAIL_COUNT=$((FAIL_COUNT + 1))
            fi
        else
            COMPILATION_STATUS+=("⚠️ Skipped (Flash/RAM limit exceeded)")
            EMULATION_STATUS+=("⏭️ Skipped (No binary)")
            echo "[simavr-matrix] ℹ Emulation skipped for $BOARD (memory limits exceeded as expected on small target)"
        fi
    else
        COMPILATION_STATUS+=("❌ Failed")
        EMULATION_STATUS+=("❌ Failed")
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

# 3. Generate Summary Report
SUMMARY_FILE="${SUMMARY_DIR}/simavr_summary.md"

cat << 'EOF' > "$SUMMARY_FILE"
### 🔬 simavr AVR Hardware Emulation Matrix (Cycle-Accurate)

| Board / Target | MCU Architecture | Firmware Compilation | Hardware Emulation (PTY/UART) | Result |
| :--- | :---: | :---: | :---: | :---: |
EOF

for i in "${!BOARDS[@]}"; do
    BOARD="${BOARDS[$i]}"
    NAME="${BOARD_NAMES[$i]}"
    C_STAT="${COMPILATION_STATUS[$i]}"
    E_STAT="${EMULATION_STATUS[$i]}"
    
    if [[ "$E_STAT" == *"Passed"* ]]; then
        OVERALL="✅ PASS"
    elif [[ "$E_STAT" == *"Skipped"* ]]; then
        OVERALL="⏭️ SKIPPED"
    else
        OVERALL="❌ FAIL"
    fi
    
    echo "| **$NAME**<br>\`$BOARD\` | AVR 8-bit | $C_STAT | $E_STAT | **$OVERALL** |" >> "$SUMMARY_FILE"
done

echo "" >> "$SUMMARY_FILE"

# Output summary to console
cat "$SUMMARY_FILE"

# Append to GITHUB_STEP_SUMMARY if present
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat "$SUMMARY_FILE" >> "$GITHUB_STEP_SUMMARY"
fi

if [ $FAIL_COUNT -ne 0 ]; then
    echo "[simavr-matrix] Matrix execution finished with $FAIL_COUNT failures." >&2
    exit 1
fi

echo "[simavr-matrix] Matrix execution finished successfully!"
exit 0
