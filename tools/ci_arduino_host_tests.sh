#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"

BUILD_DIR="${LIB_DIR}/build-host-local"
OBJ_DIR="${BUILD_DIR}/objs"
mkdir -p "${OBJ_DIR}"

# Use the python from the current environment
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    PYTHON_CMD=$(command -v python || command -v python3)
elif [[ -x "${ROOT_DIR}/.tox/py313/bin/python" ]]; then
    PYTHON_CMD="${ROOT_DIR}/.tox/py313/bin/python"
else
    PYTHON_CMD=$(command -v python3 || command -v python)
fi

# [SIL-2] Ensure dependencies are present
echo "[host-cpp] Generating protocol bindings..."
${PYTHON_CMD} "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/spec.toml" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_DIR}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_DIR}/protocol/rpc_structs.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"

echo "[host-cpp] Installing library dependencies..."
DUMMY_ARDUINO_LIBS=${DUMMY_ARDUINO_LIBS:-$(mktemp -d)}
"${LIB_DIR}/tools/install.sh" "${DUMMY_ARDUINO_LIBS}"

# Get standard library path
if [ -d "${DUMMY_ARDUINO_LIBS}" ]; then
    ARDUINO_LIBS="${DUMMY_ARDUINO_LIBS}"
else
    ARDUINO_LIBS="$HOME/Arduino/libraries"
    if [ ! -d "$ARDUINO_LIBS" ]; then
        ARDUINO_LIBS="$HOME/Documents/Arduino/libraries"
    fi
fi

# Define explicit include paths for official libraries
ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl"
PACKETSERIAL_PATH="$ARDUINO_LIBS/PacketSerial"

if [[ "${1:-}" == "--install-only" ]]; then
    echo "[host-cpp] Dependencies installed. Exiting as requested by --install-only."
    exit 0
fi

SOURCES=(
    "${SRC_DIR}/nanopb/pb_common.c"
    "${SRC_DIR}/nanopb/pb_encode.c"
    "${SRC_DIR}/nanopb/pb_decode.c"
    "${SRC_DIR}/protocol/mcubridge.pb.c"
    "${SRC_DIR}/security/security.cpp"
    "$WOLFSSL_PATH/wolfcrypt/src/sha256.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hmac.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hash.c"
    "$WOLFSSL_PATH/wolfcrypt/src/kdf.c"
    "$WOLFSSL_PATH/wolfcrypt/src/error.c"
    "$WOLFSSL_PATH/wolfcrypt/src/logging.c"
    "$WOLFSSL_PATH/wolfcrypt/src/wc_port.c"
    "$WOLFSSL_PATH/wolfcrypt/src/memory.c"
    "${SRC_DIR}/hal/hal.cpp"
    "${SRC_DIR}/protocol/rle.cpp"
    "${SRC_DIR}/Bridge.cpp"
    "${SRC_DIR}/services/Console.cpp"
    "${SRC_DIR}/services/DataStore.cpp"
    "${SRC_DIR}/services/Mailbox.cpp"
    "${SRC_DIR}/services/FileSystem.cpp"
    "${SRC_DIR}/services/Process.cpp"
    "${SRC_DIR}/services/SPIService.cpp"
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp"
)

# Unity test framework
UNITY_DIR="${TEST_DIR}/Unity"
UNITY_OBJ="${OBJ_DIR}/unity.o"
if [ -f "${UNITY_DIR}/unity.c" ]; then
    gcc -c -O2 -DUNITY_INCLUDE_DOUBLE "${UNITY_DIR}/unity.c" -o "${UNITY_OBJ}"
else
    echo "[WARN] Unity not found at ${UNITY_DIR}; test assertions will fail."
    UNITY_OBJ=""
fi

# Base flags without -std for C compatibility
BASE_FLAGS=(
    -O2
    -g
    -DBRIDGE_HOST_TEST=1
    -DBRIDGE_TEST_NO_GLOBALS=1
    -DWOLFSSL_USER_SETTINGS
    -DETL_NO_STL
    -DUNITY_INCLUDE_DOUBLE
    -I"${SRC_DIR}"
    -I"${SRC_DIR}/config"
    -I"${SRC_DIR}/nanopb"
    -I"${SRC_DIR}/protocol"
    -I"${TEST_DIR}/Unity"
    -I"${STUB_DIR}"
    -I"$ETL_PATH"
    -I"$ETL_PATH/include"
    -I"$ETL_PATH/arduino"
    -I"$WOLFSSL_PATH"
    -I"$PACKETSERIAL_PATH"
    -I"$PACKETSERIAL_PATH/src"
)

# Compile common sources to objects in parallel
echo "[host-cpp] Compiling common sources in parallel..."
OBJECTS=()
for src in "${SOURCES[@]}"; do
    obj_name=$(basename "${src}")
    obj="${OBJ_DIR}/${obj_name}.o"
    OBJECTS+=("${obj}")
    
    if [[ "${src}" == *.c ]]; then
        gcc "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}" &
    else
        g++ -std=c++17 "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}" &
    fi
done
wait

# Test suites
TEST_FILES=(
    "${TEST_DIR}/test_integrated.cpp"
    "${TEST_DIR}/test_bridge_core.cpp"
    "${TEST_DIR}/test_bridge_components.cpp"
    "${TEST_DIR}/test_host_filesystem.cpp"
    "${TEST_DIR}/test_protocol.cpp"
    "${TEST_DIR}/test_fsm_mutual_auth.cpp"
    "${TEST_DIR}/test_arduino_100_coverage.cpp"
)

# Compile and run test suites in parallel
echo "[host-cpp] Compiling and running test suites in parallel..."
pids=()
for test_file in "${TEST_FILES[@]}"; do
    (
        test_name=$(basename "${test_file}" .cpp)
        g++ -std=c++17 "${BASE_FLAGS[@]}" "${test_file}" "${OBJECTS[@]}" "${UNITY_OBJ}" -o "${BUILD_DIR}/${test_name}"
        "${BUILD_DIR}/${test_name}"
    ) &
    pids+=($!)
    
    # Throttle to MAX_JOBS
    if [[ ${#pids[@]} -ge 5 ]]; then
        wait ${pids[0]}
        pids=("${pids[@]:1}")
    fi
done

# Wait for remaining
for pid in "${pids[@]}"; do
    wait "$pid"
done

echo "[host-cpp] ALL HOST TESTS PASSED"
