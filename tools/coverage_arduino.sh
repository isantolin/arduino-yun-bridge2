#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/mcubridge-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"
BUILD_DIR="${ROOT_DIR}/coverage/build-arduino"
OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
ARDUINO_COVERAGE_MIN_LINE="${ARDUINO_COVERAGE_MIN_LINE:-95}"
ARDUINO_COVERAGE_MIN_BRANCH="${ARDUINO_COVERAGE_MIN_BRANCH:-95}"
ARDUINO_COVERAGE_RUN_ID="${ARDUINO_COVERAGE_RUN_ID:-$$}"

BUILD_DIR="${ROOT_DIR}/coverage/build-arduino-${ARDUINO_COVERAGE_RUN_ID}"

# Recreate build directory to avoid stale gcov/gcno references from old headers.
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/objs"
mkdir -p "${OUTPUT_ROOT}"

# Setup dependencies in .dummy_libs
echo "[coverage_arduino] Installing library dependencies..."
mkdir -p "${ROOT_DIR}/.dummy_libs"
"${LIB_ROOT}/tools/install.sh" "${ROOT_DIR}/.dummy_libs"

ETL_PATH="${ROOT_DIR}/.dummy_libs/Embedded_Template_Library"
WOLFSSL_PATH="${ROOT_DIR}/.dummy_libs/wolfSSL"
PACKETSERIAL_PATH="${ROOT_DIR}/.dummy_libs/PacketSerial"

# Clean old coverage data
find "${BUILD_DIR}" -name "*.gcda" -delete

# First-party sources compiled with coverage instrumentation.
BRIDGE_SOURCES=(
    "${SRC_ROOT}/security/security.cpp"
    "${SRC_ROOT}/hal/hal.cpp"
    "${SRC_ROOT}/fsm/bridge_fsm.cpp"
    "${SRC_ROOT}/Bridge.cpp"
    "${TEST_ROOT}/bridge_test_global.cpp"
    "${SRC_ROOT}/Instantiations.cpp"
    "${SRC_ROOT}/services/Console.cpp"
    "${SRC_ROOT}/services/DataStore.cpp"
    "${SRC_ROOT}/services/Mailbox.cpp"
    "${SRC_ROOT}/services/FileSystem.cpp"
    "${SRC_ROOT}/services/Process.cpp"
    "${SRC_ROOT}/services/SPIService.cpp"
    "${ROOT_DIR}/tools/arduino_stub/BridgeFaultInjection.cpp"
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp"
)

# Third-party sources compiled WITHOUT coverage instrumentation.
THIRD_PARTY_SOURCES=(
    "${WOLFSSL_PATH}/wolfcrypt/src/sha256.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/hmac.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/hash.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/kdf.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/error.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/logging.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/wc_port.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/memory.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/chacha.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/poly1305.c"
    "${WOLFSSL_PATH}/wolfcrypt/src/chacha20_poly1305.c"
    "${SRC_ROOT}/pb_encode.c"
    "${SRC_ROOT}/pb_decode.c"
    "${SRC_ROOT}/pb_common.c"
    "${SRC_ROOT}/protocol/mcubridge.pb.c"
)

# Flags with coverage instrumentation (first-party code only).
BASE_FLAGS=(
    "-O0" "-g" "-fprofile-arcs" "-ftest-coverage" "-fPIC"
    "-Wall" "-Wextra" "-Werror"
    "-DARDUINO=100" "-DBRIDGE_HOST_TEST=1" "-DWOLFSSL_USER_SETTINGS"
    "-DETL_NO_STL" "-DBRIDGE_DEBUG_IO=1" "-DBRIDGE_FAULT_INJECTION=1"
    "-DARDUINO_STUB_CUSTOM_MILLIS=1"
    "-DBRIDGE_ENABLE_CONSOLE=1" "-DBRIDGE_ENABLE_DATASTORE=1"
    "-DBRIDGE_ENABLE_MAILBOX=1" "-DBRIDGE_ENABLE_FILESYSTEM=1"
    "-DBRIDGE_ENABLE_PROCESS=1" "-DBRIDGE_ENABLE_SPI=1"
    "-DUNITY_INCLUDE_DOUBLE"
    "-I${SRC_ROOT}" "-I${SRC_ROOT}/config" "-I${SRC_ROOT}/protocol"
    "-I${STUB_INCLUDE}" "-I${TEST_ROOT}"
    "-I${ETL_PATH}" "-I${ETL_PATH}/include" "-I${ETL_PATH}/arduino"
    "-I${WOLFSSL_PATH}"
    "-I${PACKETSERIAL_PATH}" "-I${PACKETSERIAL_PATH}/src"
    "-I${TEST_ROOT}/mocks" "-I${TEST_ROOT}/Unity/src"
)

# Flags without coverage instrumentation (vendor/third-party code).
TP_FLAGS=(
    "-O2" "-g" "-fPIC"
    "-DARDUINO=100" "-DBRIDGE_HOST_TEST=1" "-DWOLFSSL_USER_SETTINGS"
    "-DETL_NO_STL"
    "-I${SRC_ROOT}" "-I${SRC_ROOT}/config" "-I${SRC_ROOT}/protocol"
    "-I${STUB_INCLUDE}" "-I${TEST_ROOT}"
    "-I${ETL_PATH}" "-I${ETL_PATH}/include" "-I${ETL_PATH}/arduino"
    "-I${WOLFSSL_PATH}"
    "-I${PACKETSERIAL_PATH}" "-I${PACKETSERIAL_PATH}/src"
)

OBJECTS=()
for src in "${BRIDGE_SOURCES[@]}"; do
    obj="${BUILD_DIR}/objs/$(basename "${src}").o"
    if [[ "${src}" == *.cpp ]]; then
        g++ -std=c++17 "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}"
    else
        gcc "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}"
    fi
    OBJECTS+=("${obj}")
done

MOCK_OBJ="${BUILD_DIR}/objs/test_host_filesystem_mock.cpp.o"
g++ -std=c++17 "${BASE_FLAGS[@]}" -c "${TEST_ROOT}/test_host_filesystem_mock.cpp" -o "${MOCK_OBJ}"

for src in "${THIRD_PARTY_SOURCES[@]}"; do
    obj="${BUILD_DIR}/objs/$(basename "${src}").o"
    gcc "${TP_FLAGS[@]}" -c "${src}" -o "${obj}"
    OBJECTS+=("${obj}")
done

UNITY_OBJ="${BUILD_DIR}/objs/unity.o"
gcc "${BASE_FLAGS[@]}" -c "${TEST_ROOT}/Unity/src/unity.c" -o "${UNITY_OBJ}"

TEST_SUITES=(
    "test_arduino_100_coverage"
    "test_integrated"
    "test_bridge_core"
    "test_bridge_components"
    "test_host_filesystem"
    "test_protocol"
    "test_fsm_mutual_auth"
    "test_coverage_full"
    "test_coverage_hardened"
    "test_bridge_edge_paths"
    "test_hal_weak_defaults"
    "test_arduino_harden"
    "test_arduino_crypto_harden"
    "test_arduino_stress"
)

echo "[coverage_arduino] Compilando y ejecutando suites..."

pushd "${BUILD_DIR}" > /dev/null
for suite in "${TEST_SUITES[@]}"; do
    suite_src="${TEST_ROOT}/${suite}.cpp"
    suite_bin="${BUILD_DIR}/${suite}"
    if [ "${suite}" = "test_hal_weak_defaults" ]; then
        g++ -std=c++17 "${BASE_FLAGS[@]}" "${suite_src}" "${OBJECTS[@]}" "${UNITY_OBJ}" -o "${suite_bin}"
    else
        g++ -std=c++17 "${BASE_FLAGS[@]}" "${suite_src}" "${OBJECTS[@]}" "${MOCK_OBJ}" "${UNITY_OBJ}" -o "${suite_bin}"
    fi
    "${suite_bin}"
done
popd > /dev/null

echo "[coverage_arduino] Generando informes finales..."
python -m gcovr --root "${SRC_ROOT}" "${BUILD_DIR}" --filter "${SRC_ROOT}" -e ".*\\.h$" -e ".*etl.*" -e ".*wolfssl.*" -e ".*wolfcrypt.*" -e ".*rpc_protocol\.h" -e ".*rpc_structs\.h" --exclude-unreachable-branches --exclude-throw-branches --merge-mode-functions=merge-use-line-max --sort uncovered-percent --fail-under-line "${ARDUINO_COVERAGE_MIN_LINE}" --fail-under-branch "${ARDUINO_COVERAGE_MIN_BRANCH}" --html-details "${OUTPUT_ROOT}/index.html" --json-summary "${OUTPUT_ROOT}/summary.json" --json-summary-pretty --json "${OUTPUT_ROOT}/coverage.json" --print-summary > "${OUTPUT_ROOT}/summary.txt"

cat "${OUTPUT_ROOT}/summary.txt"
echo "[coverage_arduino] Proceso finalizado."
