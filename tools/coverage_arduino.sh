#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/mcubridge-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"
OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
BUILD_DIR="${LIB_ROOT}/build-coverage"

# Use the python from the current environment (e.g. tox virtualenv)
PYTHON_CMD=$(command -v python || command -v python3)

# Completely clean previous build artifacts to prevent path mismatches (.gcno/.gcda)
rm -rf "${BUILD_DIR}"
mkdir -p "${OUTPUT_ROOT}" "${BUILD_DIR}"

# [SIL-2] Library Installation (Dependencies)
echo "[coverage_arduino] Generating protocol bindings..."
if ! ${PYTHON_CMD} "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/spec.toml" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_ROOT}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_ROOT}/protocol/rpc_structs.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"; then
    echo "ERROR: Protocol generation failed. See above for missing dependencies."
    exit 1
fi

"${ROOT_DIR}/tools/ci_arduino_host_tests.sh" --install-only

# Sources to track for coverage
SOURCES=(
    "${SRC_ROOT}/nanopb/pb_common.c"
    "${SRC_ROOT}/nanopb/pb_encode.c"
    "${SRC_ROOT}/nanopb/pb_decode.c"
    "${SRC_ROOT}/protocol/mcubridge.pb.c"
    "${SRC_ROOT}/security/sha256.cpp"
    "${SRC_ROOT}/security/security.cpp"
    "${SRC_ROOT}/hal/hal.cpp"
    "${SRC_ROOT}/protocol/rle.cpp"
    "${SRC_ROOT}/protocol/rpc_cobs.cpp"
    "${SRC_ROOT}/Bridge.cpp"
    "${SRC_ROOT}/services/Console.cpp"
    "${SRC_ROOT}/services/DataStore.cpp"
    "${SRC_ROOT}/services/Mailbox.cpp"
    "${SRC_ROOT}/services/FileSystem.cpp"
    "${SRC_ROOT}/services/Process.cpp"
)

# Unity test framework
UNITY_DIR="${TEST_ROOT}/Unity"
UNITY_OBJ="${BUILD_DIR}/unity.o"
if [ -f "${UNITY_DIR}/unity.c" ]; then
    gcc -c -O0 -g -fprofile-arcs -ftest-coverage -DUNITY_INCLUDE_DOUBLE "${UNITY_DIR}/unity.c" -o "${UNITY_OBJ}"
else
    echo "ERROR: Unity not found at ${UNITY_DIR}; run install.sh first."
    exit 1
fi

# Compiler flags
CXXFLAGS=(
    "-std=c++14"
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
    "-DUNITY_INCLUDE_DOUBLE"
    "-I${SRC_ROOT}"
    "-I${SRC_ROOT}/nanopb"
    "-I${SRC_ROOT}/protocol"
    "-I${STUB_INCLUDE}"
    "-I${TEST_ROOT}/mocks"
    "-I${TEST_ROOT}/Unity"
)

# [SIL-2] All test suites contribute to coverage via cumulative .gcda
TEST_SUITES=(
    "test_integrated"
    "test_bridge_core"
    "test_bridge_components"
    "test_protocol"
    "test_fsm_mutual_auth"
    "test_extreme_coverage"
    "test_extreme_coverage_v2"
    "test_arduino_100_coverage"
    "test_coverage_100_final"
    "test_coverage_gaps"
    "test_coverage_extra_gaps"
)

echo "[coverage_arduino] Compilando y ejecutando suites..."

pushd "${BUILD_DIR}" > /dev/null
for suite in "${TEST_SUITES[@]}"; do
    echo "  -> Procesando ${suite}..."
    suite_src="${TEST_ROOT}/${suite}.cpp"
    suite_bin="${BUILD_DIR}/${suite}"
    
    # Compile suite including all required sources
    # Run from BUILD_DIR so .gcno/.gcda files land here
    g++ "${CXXFLAGS[@]}" "${suite_src}" "${SOURCES[@]}" ${UNITY_OBJ} -o "${suite_bin}"
    
    # Execute
    "${suite_bin}"
done
popd > /dev/null

echo "[coverage_arduino] Generando informes finales..."
gcovr --root "${SRC_ROOT}" "${BUILD_DIR}" --filter "${SRC_ROOT}" --merge-mode-functions=merge-use-line-max --html-details "${OUTPUT_ROOT}/index.html" --json-summary "${OUTPUT_ROOT}/summary.json" --json-summary-pretty --json "${OUTPUT_ROOT}/coverage.json" --print-summary > "${OUTPUT_ROOT}/summary.txt"

# Optional: also output term summary
cat "${OUTPUT_ROOT}/summary.txt"
echo "[coverage_arduino] Proceso finalizado."
