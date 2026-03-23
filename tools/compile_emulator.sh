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
ARDUINO_LIBS="$HOME/Arduino/libraries"
if [ ! -d "$ARDUINO_LIBS" ]; then
    ARDUINO_LIBS="$HOME/Documents/Arduino/libraries"
fi

ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library_ETL/src"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl/src"

# Use the python from the current environment (e.g. tox virtualenv)
PYTHON_CMD=$(command -v python || command -v python3)

echo "[emulator] Generating protocol bindings..."
if ! ${PYTHON_CMD} "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/spec.toml" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_DIR}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_DIR}/protocol/rpc_structs.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"; then
    echo "ERROR: Protocol generation failed. See above for missing dependencies."
    exit 1
fi

echo "[emulator] Installing library dependencies..."
DUMMY_ARDUINO_LIBS=${DUMMY_ARDUINO_LIBS:-$(mktemp -d)}
"${LIB_DIR}/tools/install.sh" "${DUMMY_ARDUINO_LIBS}"

WOLF_SOURCES=(
    "$WOLFSSL_PATH/wolfcrypt/src/sha256.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hmac.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hash.c"
    "$WOLFSSL_PATH/wolfcrypt/src/kdf.c"
    "$WOLFSSL_PATH/wolfcrypt/src/error.c"
    "$WOLFSSL_PATH/wolfcrypt/src/logging.c"
    "$WOLFSSL_PATH/wolfcrypt/src/wc_port.c"
    "$WOLFSSL_PATH/wolfcrypt/src/memory.c"
)

echo "[emulator] Compiling native bridge emulator (Base)..."
g++ -std=c++17 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6  -DWOLFSSL_USER_SETTINGS -DETL_NO_STL \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${SRC_DIR}/nanopb" \
    -I"${TEST_DIR}/mocks" \
    -I"${STUB_DIR}" \
    -I"${ETL_PATH}" \
    -I"${WOLFSSL_PATH}" \
    "${WOLF_SOURCES[@]}" \
    "${SRC_DIR}/nanopb/pb_common.c" \
    "${SRC_DIR}/nanopb/pb_encode.c" \
    "${SRC_DIR}/nanopb/pb_decode.c" \
    "${SRC_DIR}/protocol/mcubridge.pb.c" \
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
    "${SRC_DIR}/services/SPIService.cpp" \
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp" \
    "${TEST_DIR}/bridge_emulator.cpp" \
    -o "${TEST_DIR}/bridge_emulator"

echo "[emulator] Compiling native bridge emulator (BridgeControl Sketch)..."
g++ -std=c++17 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6  -DWOLFSSL_USER_SETTINGS -DETL_NO_STL \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${SRC_DIR}/nanopb" \
    -I"${TEST_DIR}/mocks" \
    -I"${STUB_DIR}" \
    -I"${ETL_PATH}" \
    -I"${WOLFSSL_PATH}" \
    "${WOLF_SOURCES[@]}" \
    "${SRC_DIR}/nanopb/pb_common.c" \
    "${SRC_DIR}/nanopb/pb_encode.c" \
    "${SRC_DIR}/nanopb/pb_decode.c" \
    "${SRC_DIR}/protocol/mcubridge.pb.c" \
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
    "${SRC_DIR}/services/SPIService.cpp" \
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp" \
    "${TEST_DIR}/bridge_control_emulator.cpp" \
    -o "${TEST_DIR}/bridge_control_emulator"

if [ -f "${TEST_DIR}/bridge_emulator" ] && [ -f "${TEST_DIR}/bridge_control_emulator" ]; then
    echo "[emulator] SUCCESS: Binaries generated in ${TEST_DIR}"
else
    echo "[emulator] ERROR: Failed to generate one or more binaries"
    exit 1
fi
