#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"

BUILD_DIR="${LIB_DIR}/build-host-local"
mkdir -p "${BUILD_DIR}"

# Use the python from the current environment (e.g. tox virtualenv)
PYTHON_CMD=$(command -v python || command -v python3)

# [SIL-2] Ensure dependencies are present (ETL is required in src/etl)
echo "[host-cpp] Generating protocol bindings..."
if ! ${PYTHON_CMD} "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/spec.toml" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_DIR}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_DIR}/protocol/rpc_structs.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"; then
    echo "ERROR: Protocol generation failed. See above for missing dependencies."
    exit 1
fi

echo "[host-cpp] Installing library dependencies..."
DUMMY_ARDUINO_LIBS=${DUMMY_ARDUINO_LIBS:-$(mktemp -d)}
"${LIB_DIR}/tools/install.sh" "${DUMMY_ARDUINO_LIBS}"

if [[ "${1:-}" == "--install-only" ]]; then
    echo "[host-cpp] Dependencies installed. Exiting as requested by --install-only."
    exit 0
fi

SOURCES=(
    "${SRC_DIR}/security/sha256.cpp"
    "${SRC_DIR}/security/security.cpp"
    "${SRC_DIR}/hal/hal.cpp"
    "${SRC_DIR}/protocol/rle.cpp"
    "${SRC_DIR}/protocol/rpc_cobs.cpp"
    "${SRC_DIR}/Bridge.cpp"
    "${SRC_DIR}/services/Console.cpp"
    "${SRC_DIR}/services/DataStore.cpp"
    "${SRC_DIR}/services/Mailbox.cpp"
    "${SRC_DIR}/services/FileSystem.cpp"
    "${SRC_DIR}/services/Process.cpp"
)

# Unity test framework (compiled as C, linked with C++ tests)
UNITY_DIR="${TEST_DIR}/Unity"
UNITY_OBJ="${BUILD_DIR}/unity.o"
if [ -f "${UNITY_DIR}/unity.c" ]; then
    gcc -c -O0 -g -DUNITY_INCLUDE_DOUBLE "${UNITY_DIR}/unity.c" -o "${UNITY_OBJ}"
else
    echo "[WARN] Unity not found at ${UNITY_DIR}; test assertions will fail."
    UNITY_OBJ=""
fi

# [SIL-2] Automatically discover all test suites
TEST_FILES=(
    "${TEST_DIR}/test_integrated.cpp"
    "${TEST_DIR}/test_bridge_core.cpp"
    "${TEST_DIR}/test_bridge_components.cpp"
    "${TEST_DIR}/test_protocol.cpp"
    "${TEST_DIR}/test_fsm_mutual_auth.cpp"
    "${TEST_DIR}/test_extreme_coverage.cpp"
    "${TEST_DIR}/test_extreme_coverage_v2.cpp"
    "${TEST_DIR}/test_arduino_100_coverage.cpp"
    "${TEST_DIR}/test_arduino_coverage_boost.cpp"
    "${TEST_DIR}/test_coverage_100_final.cpp"
    "${TEST_DIR}/test_coverage_extra_gaps.cpp"
    "${TEST_DIR}/test_coverage_final_push.cpp"
    "${TEST_DIR}/test_coverage_gaps.cpp"
    "${TEST_DIR}/test_coverage_mega.cpp"
)

COMPILE_FLAGS=(
    -std=c++11
    -O0
    -g
    -DBRIDGE_HOST_TEST=1
    -DBRIDGE_TEST_NO_GLOBALS=1
    -DUNITY_INCLUDE_DOUBLE
    -I"${SRC_DIR}"
    -I"${TEST_DIR}/mocks"
    -I"${TEST_DIR}/Unity"
    -I"${STUB_DIR}"
    -I"${DUMMY_ARDUINO_LIBS}/PacketSerial"
)

echo "[host-cpp] Compiling and running all test suites..."
for test_file in "${TEST_FILES[@]}"; do
    test_name=$(basename "${test_file}" .cpp)
    echo "  -> Processing ${test_name}..."
    g++ "${COMPILE_FLAGS[@]}" "${SOURCES[@]}" "${test_file}" ${UNITY_OBJ} -o "${BUILD_DIR}/${test_name}"
    "${BUILD_DIR}/${test_name}"
done

echo "[host-cpp] ALL HOST TESTS PASSED"