#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="${ROOT_DIR}/openwrt-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"

BUILD_DIR="${LIB_DIR}/build-host-local"
mkdir -p "${BUILD_DIR}"

# [SIL-2] Ensure dependencies are present (ETL is required in src/etl)
echo "[host-cpp] Installing library dependencies..."
DUMMY_ARDUINO_LIBS=$(mktemp -d)
"${LIB_DIR}/tools/install.sh" "${DUMMY_ARDUINO_LIBS}"

SOURCES=(
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
    "${TEST_DIR}/test_coverage_100_final.cpp"
)

COMPILE_FLAGS=(
    -std=c++11
    -O0
    -g
    -DBRIDGE_HOST_TEST=1
    -DBRIDGE_TEST_NO_GLOBALS=1
    -I"${SRC_DIR}"
    -I"${TEST_DIR}/mocks"
    -I"${STUB_DIR}"
    -I"${DUMMY_ARDUINO_LIBS}/Crypto"
    -I"${DUMMY_ARDUINO_LIBS}/PacketSerial"
)

echo "[host-cpp] Compiling and running all test suites..."
for test_file in "${TEST_FILES[@]}"; do
    test_name=$(basename "${test_file}" .cpp)
    echo "  -> Processing ${test_name}..."
    g++ "${COMPILE_FLAGS[@]}" "${SOURCES[@]}" "${test_file}" -o "${BUILD_DIR}/${test_name}"
    "${BUILD_DIR}/${test_name}"
done

echo "[host-cpp] ALL HOST TESTS PASSED"