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

ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
PIGWEED_PATH="$ARDUINO_LIBS/PigweedAVR"
MBEDTLS_PATH="$ARDUINO_LIBS/MbedTLS/src"
mapfile -t MBEDTLS_SOURCES < <(find "$MBEDTLS_PATH/tf-psa-crypto" -type f -name '*.c' -print | sort)
if [[ ${#MBEDTLS_SOURCES[@]} -eq 0 ]]; then
    echo "[emulator] ERROR: No TF-PSA-Crypto sources found at $MBEDTLS_PATH" >&2
    exit 1
fi

mkdir -p "${METADATA_DIR}"
"${GEN_PYTHON}" "${ROOT_DIR}/tools/arduino_core_metadata.py" \
    --host-test \
    --write-header "${METADATA_DIR}/ArduinoCoreMetadata.h" \
    --json-output "${METADATA_DIR}/arduino_core_metadata.json"

echo "[emulator] Verifying library paths..."
ls -la "${PIGWEED_PATH}" || echo "PIGWEED_PATH not found"
ls -la "${PIGWEED_PATH}/src" || echo "PIGWEED_PATH/src not found"

NANOPB_SOURCES=(
    "${SRC_DIR}/pb_encode.c"
    "${SRC_DIR}/pb_decode.c"
    "${SRC_DIR}/pb_common.c"
    "${SRC_DIR}/protocol/config_schema.pb.c"
    "${SRC_DIR}/protocol/mcu_wire.pb.c"
)

MBEDTLS_OBJ_DIR="${METADATA_DIR}/mbedtls"
mkdir -p "$MBEDTLS_OBJ_DIR"
MBEDTLS_OBJECTS=()
for source in "${MBEDTLS_SOURCES[@]}"; do
    object_name="${source#"$MBEDTLS_PATH/"}"
    object_name="${object_name//\//_}"
    object="${MBEDTLS_OBJ_DIR}/${object_name%.c}.o"
    gcc -std=c99 -O2 -g -Wall -Wextra -Werror \
        -DBRIDGE_HOST_TEST=1 -DARDUINO=100 \
        -DETL_NO_STL -DETL_FORCE_STD_INITIALIZER_LIST \
        -I"${MBEDTLS_PATH}" \
        -c "$source" -o "$object"
    MBEDTLS_OBJECTS+=("$object")
done

echo "[emulator] Compiling native bridge emulator (Base)..."
g++ -std=c++17 -O2 -g -Wall -Wextra -Werror -DBRIDGE_HOST_TEST=1 -DARDUINO=100 -DARDUINO_STUB_CUSTOM_MILLIS=1 -DARDUINO_STUB_CUSTOM_SERIAL=1 \
    -DETL_NO_STL -DETL_FORCE_STD_INITIALIZER_LIST \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${TEST_DIR}/mocks" \
    -I"${METADATA_DIR}" \
    -I"${STUB_DIR}" \
    -I"${TEST_DIR}" \
    -I"${ETL_PATH}" \
    -I"${ETL_PATH}/include" \
    -I"${ETL_PATH}/arduino" \
    -I"${PIGWEED_PATH}" \
    -I"${PIGWEED_PATH}/src" \
    -I"${MBEDTLS_PATH}" \
    "${NANOPB_SOURCES[@]}" \
    "${MBEDTLS_OBJECTS[@]}" \
    "$PIGWEED_PATH/src/cxx_abi_stubs.cpp" \
    "$PIGWEED_PATH/src/assert_avr.cpp" \
    "$PIGWEED_PATH/src/system_clock_avr.cpp" \
    "$PIGWEED_PATH/src/log_avr.cpp" \
    "$PIGWEED_PATH/src/rpc_server.cpp" \
    "$PIGWEED_PATH/src/pw_checksum_crc32.cc" \
    "$PIGWEED_PATH/src/pw_hdlc_decoder.cc" \
    "$PIGWEED_PATH/src/pw_hdlc_encoder.cc" \
    "$PIGWEED_PATH/src/pw_varint_varint.cc" \
    "$PIGWEED_PATH/src/pw_status_status.cc" \
    "$PIGWEED_PATH/src/pw_tokenizer_encode_args.cc" \
    "$PIGWEED_PATH/src/pw_tokenizer_tokenize.cc" \
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
    -DETL_NO_STL -DETL_FORCE_STD_INITIALIZER_LIST \
    -I"${SRC_DIR}" \
    -I"${SRC_DIR}/config" \
    -I"${TEST_DIR}/mocks" \
    -I"${METADATA_DIR}" \
    -I"${STUB_DIR}" \
    -I"${TEST_DIR}" \
    -I"${ETL_PATH}" \
    -I"${ETL_PATH}/include" \
    -I"${ETL_PATH}/arduino" \
    -I"${PIGWEED_PATH}" \
    -I"${PIGWEED_PATH}/src" \
    -I"${MBEDTLS_PATH}" \
    "${NANOPB_SOURCES[@]}" \
    "${MBEDTLS_OBJECTS[@]}" \
    "$PIGWEED_PATH/src/cxx_abi_stubs.cpp" \
    "$PIGWEED_PATH/src/assert_avr.cpp" \
    "$PIGWEED_PATH/src/system_clock_avr.cpp" \
    "$PIGWEED_PATH/src/log_avr.cpp" \
    "$PIGWEED_PATH/src/rpc_server.cpp" \
    "$PIGWEED_PATH/src/pw_checksum_crc32.cc" \
    "$PIGWEED_PATH/src/pw_hdlc_decoder.cc" \
    "$PIGWEED_PATH/src/pw_hdlc_encoder.cc" \
    "$PIGWEED_PATH/src/pw_varint_varint.cc" \
    "$PIGWEED_PATH/src/pw_status_status.cc" \
    "$PIGWEED_PATH/src/pw_tokenizer_encode_args.cc" \
    "$PIGWEED_PATH/src/pw_tokenizer_tokenize.cc" \
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
