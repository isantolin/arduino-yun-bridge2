#!/usr/bin/env bash
#
# Compile the native Bridge Emulator for host-based E2E testing.
# This script is used both locally and in CI (GitHub Actions).
#

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"

# Find library paths (local or system)
ARDUINO_LIBS="${ARDUINO_LIB_DIR:-$HOME/Arduino/libraries}"
mkdir -p "${ARDUINO_LIBS}"

echo "[emulator] Installing library dependencies..."
"${LIB_DIR}/tools/install.sh" "${ARDUINO_LIBS}"

ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfSSL"
if [ ! -d "$WOLFSSL_PATH" ]; then WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl"; fi
if [ -d "$WOLFSSL_PATH/src/wolfcrypt/src" ]; then
    WOLFCRYPT_SRC="$WOLFSSL_PATH/src/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH/src"
else
    WOLFCRYPT_SRC="$WOLFSSL_PATH/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH"
fi
PACKETSERIAL_PATH="$ARDUINO_LIBS/PacketSerial"

# Use system Python
GEN_PYTHON=$(command -v python3 || command -v python)

echo "[emulator] Verifying library paths..."
ls -la "${PACKETSERIAL_PATH}" || echo "PACKETSERIAL_PATH not found"
ls -la "${PACKETSERIAL_PATH}/src" || echo "PACKETSERIAL_PATH/src not found"

echo "[emulator] Generating protocol bindings..."
if ! "${GEN_PYTHON}" "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/mcubridge.proto" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_DIR}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_DIR}/protocol/rpc_structs.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"; then
    echo "ERROR: Protocol generation failed. See above for missing dependencies."
    exit 1
fi

WOLF_SOURCES=(
    "$WOLFCRYPT_SRC/sha256.c"
    "$WOLFCRYPT_SRC/hmac.c"
    "$WOLFCRYPT_SRC/hash.c"
    "$WOLFCRYPT_SRC/kdf.c"
    "$WOLFCRYPT_SRC/error.c"
    "$WOLFCRYPT_SRC/logging.c"
    "$WOLFCRYPT_SRC/wc_port.c"
    "$WOLFCRYPT_SRC/memory.c"
    "$WOLFCRYPT_SRC/chacha.c"
    "$WOLFCRYPT_SRC/poly1305.c"
    "$WOLFCRYPT_SRC/chacha20_poly1305.c"
)

NANOPB_SOURCES=(
    "${SRC_DIR}/pb_encode.c"
    "${SRC_DIR}/pb_decode.c"
    "${SRC_DIR}/pb_common.c"
    "${SRC_DIR}/protocol/mcubridge.pb.c"
)

echo "[emulator] Compiling native bridge emulator (Base)..."
g++ -std=c++17 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6  -DWOLFSSL_USER_SETTINGS -DETL_NO_STL \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${TEST_DIR}/mocks" \
    -I"${STUB_DIR}" \
    -I"${TEST_DIR}" \
    -I"${ETL_PATH}" \
    -I"${ETL_PATH}/include" \
    -I"${ETL_PATH}/arduino" \
    -I"${WOLFSSL_PATH}" \
    -I"${WOLFSSL_INC}" \
    -I"${PACKETSERIAL_PATH}" \
    -I"${PACKETSERIAL_PATH}/src" \
    "${WOLF_SOURCES[@]}" \
    "${NANOPB_SOURCES[@]}" \
    "${SRC_DIR}/security/security.cpp" \
    "${SRC_DIR}/hal/hal.cpp" \
    "${SRC_DIR}/fsm/bridge_fsm.cpp" \
    "${SRC_DIR}/Bridge.cpp" \
    "${SRC_DIR}/Instantiations.cpp" \
    "${SRC_DIR}/BridgeInstance.cpp" \
    "${SRC_DIR}/services/Console.cpp" \
    "${SRC_DIR}/services/DataStore.cpp" \
    "${SRC_DIR}/services/Mailbox.cpp" \
    "${SRC_DIR}/services/FileSystem.cpp" \
    "${LIB_DIR}/src/services/Process.cpp" \
    "${LIB_DIR}/src/services/SPIService.cpp" \
    "${TEST_DIR}/test_host_filesystem_mock.cpp" \
    "${ROOT_DIR}/tools/arduino_stub/BridgeFaultInjection.cpp" \
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp" \
    "${TEST_DIR}/bridge_emulator.cpp" \
    -o "${TEST_DIR}/bridge_emulator"

echo "[emulator] Compiling native bridge emulator (BridgeControl Sketch)..."
g++ -std=c++17 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DNUM_DIGITAL_PINS=20 -DNUM_ANALOG_INPUTS=6  -DWOLFSSL_USER_SETTINGS -DETL_NO_STL \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${TEST_DIR}/mocks" \
    -I"${STUB_DIR}" \
    -I"${TEST_DIR}" \
    -I"${ETL_PATH}" \
    -I"${ETL_PATH}/include" \
    -I"${ETL_PATH}/arduino" \
    -I"${WOLFSSL_PATH}" \
    -I"${WOLFSSL_INC}" \
    -I"${PACKETSERIAL_PATH}" \
    -I"${PACKETSERIAL_PATH}/src" \
    "${WOLF_SOURCES[@]}" \
    "${NANOPB_SOURCES[@]}" \
    "${SRC_DIR}/security/security.cpp" \
    "${SRC_DIR}/hal/hal.cpp" \
    "${SRC_DIR}/fsm/bridge_fsm.cpp" \
    "${SRC_DIR}/Bridge.cpp" \
    "${SRC_DIR}/Instantiations.cpp" \
    "${SRC_DIR}/BridgeInstance.cpp" \
    "${SRC_DIR}/services/Console.cpp" \
    "${SRC_DIR}/services/DataStore.cpp" \
    "${SRC_DIR}/services/Mailbox.cpp" \
    "${SRC_DIR}/services/FileSystem.cpp" \
    "${LIB_DIR}/src/services/Process.cpp" \
    "${LIB_DIR}/src/services/SPIService.cpp" \
    "${TEST_DIR}/test_host_filesystem_mock.cpp" \
    "${ROOT_DIR}/tools/arduino_stub/BridgeFaultInjection.cpp" \
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp" \
    "${TEST_DIR}/bridge_control_emulator.cpp" \
    -o "${TEST_DIR}/bridge_control_emulator"

if [ -f "${TEST_DIR}/bridge_emulator" ] && [ -f "${TEST_DIR}/bridge_control_emulator" ]; then
    echo "[emulator] SUCCESS: Binaries generated in ${TEST_DIR}"
else
    echo "[emulator] ERROR: Failed to generate one or more binaries"
    exit 1
fi
