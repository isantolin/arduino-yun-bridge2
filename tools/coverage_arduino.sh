#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/mcubridge-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"
OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
BUILD_DIR="${LIB_ROOT}/build-coverage"

mkdir -p "${OUTPUT_ROOT}" "${BUILD_DIR}"

# [SIL-2] Library Installation (Dependencies)
"${ROOT_DIR}/tools/ci_arduino_host_tests.sh" --install-only

# Sources to track for coverage
SOURCES=(
    "${SRC_ROOT}/security/security.cpp"
    "${SRC_ROOT}/hal/hal.cpp"
    "${SRC_ROOT}/protocol/rle.cpp"
    "${SRC_ROOT}/protocol/rpc_cobs.cpp"
    "${SRC_ROOT}/protocol/rpc_protocol.cpp"
    "${SRC_ROOT}/protocol/rpc_structs.cpp"
    "${SRC_ROOT}/Bridge.cpp"
    "${SRC_ROOT}/services/Console.cpp"
    "${SRC_ROOT}/services/DataStore.cpp"
    "${SRC_ROOT}/services/Mailbox.cpp"
    "${SRC_ROOT}/services/FileSystem.cpp"
    "${SRC_ROOT}/services/Process.cpp"
)

# Compiler flags
CXXFLAGS=(
    "-std=c++11"
    "-O0"
    "-g"
    "-fprofile-arcs"
    "-ftest-coverage"
    "-fPIC"
    "-DARDUINO=100"
    "-DBRIDGE_HOST_TEST=1"
    "-DBRIDGE_TEST_NO_GLOBALS=1"
    "-DBRIDGE_DEBUG_IO=1"
    "-DBRIDGE_ENABLE_CONSOLE=1"
    "-DBRIDGE_ENABLE_DATASTORE=1"
    "-DBRIDGE_ENABLE_MAILBOX=1"
    "-DBRIDGE_ENABLE_FILESYSTEM=1"
    "-DBRIDGE_ENABLE_PROCESS=1"
    "-I${SRC_ROOT}"
    "-I${STUB_INCLUDE}"
)

# Test suites to execute
TEST_SUITES=(
    "test_integrated"
)

echo "[coverage_arduino] Compilando y ejecutando suites..."
pushd "${BUILD_DIR}" > /dev/null
for suite in "${TEST_SUITES[@]}"; do
    echo "  -> Procesando ${suite}..."
    suite_src="${TEST_ROOT}/${suite}.cpp"
    suite_bin="${BUILD_DIR}/${suite}"
    
    # Compile suite including all required sources
    # Run from BUILD_DIR so .gcno/.gcda files land here
    g++ "${CXXFLAGS[@]}" "${suite_src}" "${SOURCES[@]}" -o "${suite_bin}"
    
    # Execute
    "${suite_bin}"
done
popd > /dev/null

echo "[coverage_arduino] Generando informes finales..."
gcovr --root "${SRC_ROOT}" "${BUILD_DIR}" --filter "${SRC_ROOT}" --merge-mode-functions=merge-use-line-max --html-details "${OUTPUT_ROOT}/index.html" --json-summary "${OUTPUT_ROOT}/summary.json" --json-summary-pretty --print-summary > "${OUTPUT_ROOT}/summary.txt"

# Optional: also output term summary
cat "${OUTPUT_ROOT}/summary.txt"
echo "[coverage_arduino] Proceso finalizado."
