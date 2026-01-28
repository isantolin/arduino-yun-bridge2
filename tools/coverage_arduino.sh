#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/openwrt-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"

BUILD_DIR="${LIB_ROOT}/build-coverage"
OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
mkdir -p "${BUILD_DIR}" "${OUTPUT_ROOT}"

# [SIL-2] Ensure dependencies are present (ETL is required in src/etl)
echo "[coverage_arduino] Installing library dependencies..."
DUMMY_ARDUINO_LIBS=$(mktemp -d)
"${LIB_ROOT}/tools/install.sh" "${DUMMY_ARDUINO_LIBS}"

# Limpieza total
find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true
find "${BUILD_DIR}" -name '*.gcno' -delete 2>/dev/null || true
find "${BUILD_DIR}" -name '*.o' -delete 2>/dev/null || true

SOURCES=(
    "${SRC_ROOT}/protocol/rpc_frame.cpp"
    "${SRC_ROOT}/arduino/Bridge.cpp"
    "${SRC_ROOT}/arduino/Console.cpp"
    "${SRC_ROOT}/arduino/DataStore.cpp"
    "${SRC_ROOT}/arduino/Mailbox.cpp"
    "${SRC_ROOT}/arduino/FileSystem.cpp"
    "${SRC_ROOT}/arduino/Process.cpp"
)

COMPILE_FLAGS=(
    -std=c++11
    -g
    -O0
    -fprofile-arcs
    -ftest-coverage
    -DBRIDGE_HOST_TEST=1
    -DBRIDGE_TEST_NO_GLOBALS=1
    -DBRIDGE_ENABLE_DATASTORE=1
    -DBRIDGE_ENABLE_FILESYSTEM=1
    -DBRIDGE_ENABLE_MAILBOX=1
    -DBRIDGE_ENABLE_PROCESS=1
    -I"${SRC_ROOT}"
    -I"${TEST_ROOT}/mocks"
    -I"${STUB_INCLUDE}"
)

echo "[coverage_arduino] Compilando binario integrado..."
g++ "${COMPILE_FLAGS[@]}" "${SOURCES[@]}" "${TEST_ROOT}/test_integrated.cpp" -o "${BUILD_DIR}/test_integrated"

echo "[coverage_arduino] Ejecutando tests integrados..."
"${BUILD_DIR}/test_integrated"

echo "[coverage_arduino] Generando informes..."
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --print-summary >"${OUTPUT_ROOT}/summary.txt"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --xml "${OUTPUT_ROOT}/coverage.xml"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --html-details "${OUTPUT_ROOT}/index.html"

echo "[coverage_arduino] Reporte finalizado en ${OUTPUT_ROOT}"
