#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/mcubridge-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"
OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
BUILD_DIR="${LIB_ROOT}/build-coverage"
OBJ_DIR="${BUILD_DIR}/objs"

# Use the python from the current environment (e.g. tox virtualenv)
PYTHON_CMD=$(command -v python || command -v python3)

# Completely clean previous build artifacts to prevent path mismatches (.gcno/.gcda)
rm -rf "${BUILD_DIR}"
mkdir -p "${OUTPUT_ROOT}" "${OBJ_DIR}"

# [SIL-2] Library Installation (Dependencies)
echo "[coverage_arduino] Generating protocol bindings..."
${PYTHON_CMD} "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/spec.toml" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_ROOT}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_ROOT}/protocol/rpc_structs.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"

# Ensure DUMMY_ARDUINO_LIBS is set for CI
export DUMMY_ARDUINO_LIBS="${DUMMY_ARDUINO_LIBS:-$(mktemp -d)}"
"${ROOT_DIR}/tools/ci_arduino_host_tests.sh" --install-only

# Get standard library path
ARDUINO_LIBS="${DUMMY_ARDUINO_LIBS}"

# Define explicit include paths for official libraries
ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl"

# Sources to track for coverage
SOURCES=(
    "${SRC_ROOT}/nanopb/pb_common.c"
    "${SRC_ROOT}/nanopb/pb_encode.c"
    "${SRC_ROOT}/nanopb/pb_decode.c"
    "${SRC_ROOT}/protocol/mcubridge.pb.c"
    "${SRC_ROOT}/security/security.cpp"
    "$WOLFSSL_PATH/wolfcrypt/src/sha256.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hmac.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hash.c"
    "$WOLFSSL_PATH/wolfcrypt/src/kdf.c"
    "$WOLFSSL_PATH/wolfcrypt/src/error.c"
    "$WOLFSSL_PATH/wolfcrypt/src/logging.c"
    "$WOLFSSL_PATH/wolfcrypt/src/wc_port.c"
    "$WOLFSSL_PATH/wolfcrypt/src/memory.c"
    "${SRC_ROOT}/hal/hal.cpp"
    "${SRC_ROOT}/protocol/rle.cpp"
    "${SRC_ROOT}/protocol/rpc_cobs.cpp"
    "${SRC_ROOT}/Bridge.cpp"
    "${SRC_ROOT}/services/Console.cpp"
    "${SRC_ROOT}/services/DataStore.cpp"
    "${SRC_ROOT}/services/Mailbox.cpp"
    "${SRC_ROOT}/services/FileSystem.cpp"
    "${SRC_ROOT}/services/Process.cpp"
    "${SRC_ROOT}/services/SPIService.cpp"
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp"
)


# Unity test framework
UNITY_DIR="${TEST_ROOT}/Unity"
UNITY_OBJ="${OBJ_DIR}/unity.o"
if [ -f "${UNITY_DIR}/unity.c" ]; then
    gcc -c -O0 -g -fprofile-arcs -ftest-coverage -DUNITY_INCLUDE_DOUBLE "${UNITY_DIR}/unity.c" -o "${UNITY_OBJ}"
elif [ -f "${UNITY_DIR}/src/unity.c" ]; then
    UNITY_DIR="${TEST_ROOT}/Unity/src"
    gcc -c -O0 -g -fprofile-arcs -ftest-coverage -DUNITY_INCLUDE_DOUBLE "${UNITY_DIR}/unity.c" -o "${UNITY_OBJ}"
else
    echo "ERROR: Unity not found at ${UNITY_DIR}; run install.sh first."
    exit 1
fi

# Base compiler flags
BASE_FLAGS=(
    "-O0"
    "-g"
    "-fprofile-arcs"
    "-ftest-coverage"
    "-fPIC"
    "-DARDUINO=100"
    "-DBRIDGE_HOST_TEST=1"
    "-DBRIDGE_TEST_NO_GLOBALS=1"
    "-DWOLFSSL_USER_SETTINGS"
    "-DETL_NO_STL"
    "-DBRIDGE_DEBUG_IO=1"
    "-DBRIDGE_ENABLE_CONSOLE=1"
    "-DBRIDGE_ENABLE_DATASTORE=1"
    "-DBRIDGE_ENABLE_MAILBOX=1"
    "-DBRIDGE_ENABLE_FILESYSTEM=1"
    "-DBRIDGE_ENABLE_PROCESS=1"
    "-DBRIDGE_ENABLE_SPI=1"
    "-DUNITY_INCLUDE_DOUBLE"
    "-I${SRC_ROOT}"
    "-I${SRC_ROOT}/config"
    "-I${SRC_ROOT}/nanopb"
    "-I${SRC_ROOT}/protocol"
    "-I${STUB_INCLUDE}"
    "-I$ETL_PATH"
    "-I$ETL_PATH/include"
    "-I$ETL_PATH/arduino"
    "-I$WOLFSSL_PATH"
    "-I$WOLFSSL_PATH/src"
    "-I${TEST_ROOT}/mocks"
    "-I${TEST_ROOT}/Unity"
    "-I${TEST_ROOT}/Unity/src"
)

# Compile common sources to objects in parallel
echo "[coverage_arduino] Compiling common sources in parallel..."
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

# [SIL-2] All test suites contribute to coverage via cumulative .gcda
TEST_SUITES=(
    "test_integrated"
    "test_bridge_core"
    "test_bridge_components"
    "test_protocol"
    "test_fsm_mutual_auth"
    "test_arduino_100_coverage"
)

echo "[coverage_arduino] Compilando y ejecutando suites en paralelo..."

pushd "${BUILD_DIR}" > /dev/null
pids=()
for suite in "${TEST_SUITES[@]}"; do
    (
        suite_src="${TEST_ROOT}/${suite}.cpp"
        suite_bin="${BUILD_DIR}/${suite}"
        g++ -std=c++17 "${BASE_FLAGS[@]}" "${suite_src}" "${OBJECTS[@]}" "${UNITY_OBJ}" -o "${suite_bin}"
        "${suite_bin}"
    ) &
    pids+=($!)
    
    if [[ ${#pids[@]} -ge 5 ]]; then
        wait ${pids[0]}
        pids=("${pids[@]:1}")
    fi
done

for pid in "${pids[@]}"; do
    wait "$pid"
done
popd > /dev/null

echo "[coverage_arduino] Generando informes finales..."
gcovr --root "${SRC_ROOT}" "${BUILD_DIR}" --filter "${SRC_ROOT}" --exclude "${SRC_ROOT}/nanopb" --exclude "${SRC_ROOT}/etl" --exclude "${SRC_ROOT}/wolfssl" --exclude "${SRC_ROOT}/wolfcrypt" --merge-mode-functions=merge-use-line-max --html-details "${OUTPUT_ROOT}/index.html" --json-summary "${OUTPUT_ROOT}/summary.json" --json-summary-pretty --json "${OUTPUT_ROOT}/coverage.json" --print-summary > "${OUTPUT_ROOT}/summary.txt"

# Optional: also output term summary
cat "${OUTPUT_ROOT}/summary.txt"
echo "[coverage_arduino] Proceso finalizado."
