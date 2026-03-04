#!/usr/bin/env bash
#
# Compile the native Bridge Emulator for host-based E2E testing.
# This script is used both locally and in CI (GitHub Actions).
#

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"

# Find library paths (local or system)
ETL_PATH="${LIB_DIR}/src"
# CI environment usually has these in specific paths or we install them via install.sh
# For the emulator, we assume dependencies are already in src/ via install.sh
PACKETSERIAL_PATH="${DUMMY_ARDUINO_LIBS:-/tmp/arduino_libs}/PacketSerial/src"

# Fallback for paths if not provided via environment (local dev)
CURRENT_USER="$(whoami)"
if [ ! -d "${PACKETSERIAL_PATH}" ]; then
    PACKETSERIAL_PATH="/home/${CURRENT_USER}/Arduino/libraries/PacketSerial/src"
fi

# Use the python from the current environment (e.g. tox virtualenv)
PYTHON_CMD=$(command -v python || command -v python3)

echo "[emulator] Generating protocol bindings..."
if ! ${PYTHON_CMD} "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/spec.toml" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_DIR}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_DIR}/protocol/rpc_structs.h"; then
    echo "ERROR: Protocol generation failed. See above for missing dependencies."
    exit 1
fi

echo "[emulator] Compiling native bridge emulator (Base)..."
g++ -std=c++11 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6 \
    -I"${SRC_DIR}" \
    -I"${TEST_DIR}/mocks" \
    -I"${STUB_DIR}" \
    -I"${ETL_PATH}" \
    -I"${PACKETSERIAL_PATH}" \
    "${SRC_DIR}/security/sha256.cpp" \
    "${SRC_DIR}/security/security.cpp" \
    "${SRC_DIR}/hal/hal.cpp" \
    "${SRC_DIR}/protocol/rle.cpp" \
    "${SRC_DIR}/protocol/rpc_cobs.cpp" \
    "${SRC_DIR}/Bridge.cpp" \
    "${SRC_DIR}/services/Console.cpp" \
    "${SRC_DIR}/services/DataStore.cpp" \
    "${SRC_DIR}/services/Mailbox.cpp" \
    "${SRC_DIR}/services/FileSystem.cpp" \
    "${SRC_DIR}/services/Process.cpp" \
    "${TEST_DIR}/bridge_emulator.cpp" \
    -o "${TEST_DIR}/bridge_emulator"

echo "[emulator] Compiling native bridge emulator (BridgeControl Sketch)..."
g++ -std=c++11 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6 \
    -I"${SRC_DIR}" \
    -I"${TEST_DIR}/mocks" \
    -I"${STUB_DIR}" \
    -I"${ETL_PATH}" \
    -I"${PACKETSERIAL_PATH}" \
    "${SRC_DIR}/security/sha256.cpp" \
    "${SRC_DIR}/security/security.cpp" \
    "${SRC_DIR}/hal/hal.cpp" \
    "${SRC_DIR}/protocol/rle.cpp" \
    "${SRC_DIR}/protocol/rpc_cobs.cpp" \
    "${SRC_DIR}/Bridge.cpp" \
    "${SRC_DIR}/services/Console.cpp" \
    "${SRC_DIR}/services/DataStore.cpp" \
    "${SRC_DIR}/services/Mailbox.cpp" \
    "${SRC_DIR}/services/FileSystem.cpp" \
    "${SRC_DIR}/services/Process.cpp" \
    "${TEST_DIR}/bridge_control_emulator.cpp" \
    -o "${TEST_DIR}/bridge_control_emulator"

if [ -f "${TEST_DIR}/bridge_emulator" ] && [ -f "${TEST_DIR}/bridge_control_emulator" ]; then
    echo "[emulator] SUCCESS: Binaries generated in ${TEST_DIR}"
else
    echo "[emulator] ERROR: Failed to generate one or more binaries"
    exit 1
fi
