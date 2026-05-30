#!/bin/bash
set -euo pipefail
# [MIL-SPEC/SIL-2] McuBridge CI Fuzz Orchestrator

# Ensure emulator is compiled
./tools/compile_emulator.sh

FUZZ_PTY="/tmp/ttyBRIDGE_FUZZ"
EMULATOR_BIN="mcubridge-library-arduino/tests/bridge_control_emulator"

# Cleanup previous runs
rm -f "$FUZZ_PTY"

echo "[fuzz] Starting isolated MCU emulator via socat..."
socat -d -d PTY,link="$FUZZ_PTY",raw,echo=0 EXEC:"$EMULATOR_BIN",pty,raw,echo=0 > /tmp/socat_fuzz.log 2>&1 &
SOCAT_PID=$!

# Trap cleanup
trap 'echo "[fuzz] Cleaning up..."; kill $SOCAT_PID 2>/dev/null || true; rm -f "$FUZZ_PTY"' EXIT

# Wait for PTY to be ready
MAX_RETRIES=10
COUNT=0
while [ ! -e "$FUZZ_PTY" ]; do
    sleep 0.5
    COUNT=$((COUNT+1))
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "[ERROR] PTY device never appeared"
        cat /tmp/socat_fuzz.log
        exit 1
    fi
done

echo "[fuzz] PTY ready at $FUZZ_PTY. Starting stress campaign..."

# Run the fuzzer (1000 iterations for CI stability/time balance)
python3 tools/protocol_fuzzer.py --port "$FUZZ_PTY" --count 1000

echo "[SUCCESS] Protocol fuzzing campaign completed successfully."
