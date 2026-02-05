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

echo "[host-cpp] Building integrated test suite..."
echo "DEBUG: Current directory: $(pwd)"
echo "DEBUG: SRC_DIR: ${SRC_DIR}"
ls -F "${SRC_DIR}"
ls -F "${SRC_DIR}/etl" | head -n 5
g++ -std=c++11 -O0 -g -DBRIDGE_HOST_TEST=1 -DBRIDGE_TEST_NO_GLOBALS=1 \
    -I"${SRC_DIR}" \
    -I"${TEST_DIR}/mocks" \
    -I"${STUB_DIR}" \
    -I"${DUMMY_ARDUINO_LIBS}/Crypto" \
    -I"${DUMMY_ARDUINO_LIBS}/PacketSerial" \
    "${SRC_DIR}/protocol/rpc_frame.cpp" \
    "${SRC_DIR}/security/security.cpp" \
    "${SRC_DIR}/services/Bridge.cpp" \
    "${SRC_DIR}/services/Console.cpp" \
    "${SRC_DIR}/services/DataStore.cpp" \
    "${SRC_DIR}/services/FileSystem.cpp" \
    "${SRC_DIR}/services/Mailbox.cpp" \
    "${SRC_DIR}/services/Process.cpp" \
    "${TEST_DIR}/test_integrated.cpp" \
    -o "${BUILD_DIR}/test_integrated"

echo "[host-cpp] Running integrated tests..."
"${BUILD_DIR}/test_integrated"

echo "[host-cpp] ALL HOST TESTS PASSED"