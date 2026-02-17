#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_DIR="${ROOT_DIR}/openwrt-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"
BUILD_DIR="${LIB_DIR}/build-variants"

mkdir -p "${BUILD_DIR}"

# Ensure dependencies are installed
echo "[variant-test] Checking dependencies..."
DUMMY_ARDUINO_LIBS=$(mktemp -d)
"${LIB_DIR}/tools/install.sh" "${DUMMY_ARDUINO_LIBS}" > /dev/null

compile_variant() {
    local variant_name=$1
    local enable_val=$2
    
    echo "----------------------------------------------------------------"
    echo "Compiling Variant: ${variant_name} (ENABLE=${enable_val})"
    echo "----------------------------------------------------------------"
    
    local out_bin="${BUILD_DIR}/test_${variant_name}"
    
    g++ -std=c++11 -O0 -g -DBRIDGE_HOST_TEST=1 -DBRIDGE_TEST_NO_GLOBALS=1 \
        -DBRIDGE_ENABLE_DATASTORE=${enable_val} \
        -DBRIDGE_ENABLE_FILESYSTEM=${enable_val} \
        -DBRIDGE_ENABLE_MAILBOX=${enable_val} \
        -DBRIDGE_ENABLE_PROCESS=${enable_val} \
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
        "${SRC_DIR}/services/Process.cpp" \
        "${TEST_DIR}/test_integrated.cpp" \
        -o "${out_bin}"
        
    if [ -f "${out_bin}" ]; then
        echo "[OK] Compilation successful."
        # Run it to ensure runtime integrity
        "${out_bin}" > /dev/null
        echo "[OK] Execution successful."
    else
        echo "[FAIL] Compilation failed."
        exit 1
    fi
}

# Test 1: All Disabled (Minimal Footprint)
compile_variant "minimal" 0

# Test 2: All Enabled (Full Feature Set)
compile_variant "full" 1

echo "================================================================"
echo "[SUCCESS] Both variants compiled and executed correctly."
echo "================================================================"
rm -rf "${DUMMY_ARDUINO_LIBS}"
