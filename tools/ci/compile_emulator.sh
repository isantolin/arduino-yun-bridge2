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
METADATA_DIR="${ROOT_DIR}/build/emulation"

# Find library paths (local or system)
ARDUINO_LIBS="${ARDUINO_LIB_DIR:-$HOME/Arduino/libraries}"
mkdir -p "${ARDUINO_LIBS}"

# Use system Python
GEN_PYTHON=$(command -v python3 || command -v python)

echo "[emulator] Generating protocol bindings..."
if ! "${GEN_PYTHON}" "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/cloud_gateway.proto" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_DIR}/protocol/rpc_protocol.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"; then
    echo "ERROR: Protocol generation failed. See above for missing dependencies."
    exit 1
fi

echo "[emulator] Installing library dependencies..."
"${LIB_DIR}/tools/install.sh" "${ARDUINO_LIBS}"

# shellcheck source=tools/ci/arduino_toolchains.sh
source "${ROOT_DIR}/tools/ci/arduino_toolchains.sh"

ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl"
if [ ! -d "$WOLFSSL_PATH" ]; then WOLFSSL_PATH="$ARDUINO_LIBS/wolfSSL"; fi
if [ -d "$WOLFSSL_PATH/src/wolfcrypt/src" ]; then
    WOLFCRYPT_SRC="$WOLFSSL_PATH/src/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH/src"
else
    WOLFCRYPT_SRC="$WOLFSSL_PATH/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH"
fi
WOLF_SOURCES=(
    "$WOLFCRYPT_SRC/sha256.c"
    "$WOLFCRYPT_SRC/hmac.c"
    "$WOLFCRYPT_SRC/kdf.c"
    "$WOLFCRYPT_SRC/chacha.c"
    "$WOLFCRYPT_SRC/poly1305.c"
    "$WOLFCRYPT_SRC/chacha20_poly1305.c"
    "$WOLFCRYPT_SRC/hash.c"
    "$WOLFCRYPT_SRC/misc.c"
)

mkdir -p "${METADATA_DIR}"
"${GEN_PYTHON}" "${ROOT_DIR}/tools/arduino_core_metadata.py" \
    --host-test \
    --write-header "${METADATA_DIR}/ArduinoCoreMetadata.h" \
    --json-output "${METADATA_DIR}/arduino_core_metadata.json"


NANOPB_SOURCES=(
    "${SRC_DIR}/pb_encode.c"
    "${SRC_DIR}/pb_decode.c"
    "${SRC_DIR}/pb_common.c"
    "${SRC_DIR}/protocol/config_schema.pb.c"
    "${SRC_DIR}/protocol/mcu_wire.pb.c"
)

WOLF_OBJ_DIR="${METADATA_DIR}/wolfssl"
mkdir -p "$WOLF_OBJ_DIR"
WOLF_OBJECTS=()
for source in "${WOLF_SOURCES[@]}"; do
    object_name="${source#"$WOLFSSL_PATH/"}"
    object_name="${object_name//\//_}"
    object="${WOLF_OBJ_DIR}/${object_name%.c}.o"
    gcc -std=c99 -O2 -g -Wall -Wextra -Werror \
        -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DWOLFSSL_USER_SETTINGS \
        -DETL_NO_STL -DETL_FORCE_STD_INITIALIZER_LIST \
        -I"${WOLFSSL_INC}" \
        -I"${WOLFSSL_PATH}" \
        -c "$source" -o "$object"
    WOLF_OBJECTS+=("$object")
done

echo "[emulator] Compiling native bridge emulator (Base)..."
g++ -std=c++17 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DETL_NO_STL -DETL_FORCE_STD_INITIALIZER_LIST -DPW_RPC_USE_GLOBAL_MUTEX=0 \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${TEST_DIR}/mocks" \
    -I"${METADATA_DIR}" \
    -I"${STUB_DIR}" \
    -I"${TEST_DIR}" \
    -I"${ETL_PATH}" \
    -I"${ETL_PATH}/include" \
    -I"${ETL_PATH}/arduino" \
    "${PW_INC_FLAGS[@]}" \
    -I"${WOLFSSL_INC}" \
    -I"${WOLFSSL_PATH}" \
    -DWOLFSSL_USER_SETTINGS \
    "${NANOPB_SOURCES[@]}" \
    "${WOLF_OBJECTS[@]}" \
    "${PW_PIGWEED_SRCS[@]}" \
    "${SRC_DIR}/security/security.cpp" \
    "${SRC_DIR}/hal/hal.cpp" \
    "${SRC_DIR}/fsm/bridge_fsm.cpp" \
    "${SRC_DIR}/Bridge.cpp" \
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
    -DETL_NO_STL -DETL_FORCE_STD_INITIALIZER_LIST -DPW_RPC_USE_GLOBAL_MUTEX=0 \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${TEST_DIR}/mocks" \
    -I"${METADATA_DIR}" \
    -I"${STUB_DIR}" \
    -I"${TEST_DIR}" \
    -I"${ETL_PATH}" \
    -I"${ETL_PATH}/include" \
    -I"${ETL_PATH}/arduino" \
    "${PW_INC_FLAGS[@]}" \
    -I"${WOLFSSL_INC}" \
    -I"${WOLFSSL_PATH}" \
    -DWOLFSSL_USER_SETTINGS \
    "${NANOPB_SOURCES[@]}" \
    "${WOLF_OBJECTS[@]}" \
    "${PW_PIGWEED_SRCS[@]}" \
    "${SRC_DIR}/security/security.cpp" \
    "${SRC_DIR}/hal/hal.cpp" \
    "${SRC_DIR}/fsm/bridge_fsm.cpp" \
    "${SRC_DIR}/Bridge.cpp" \
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
