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

# [SIL-2] Use local stubs for host coverage (offline-safe).
echo "[coverage_arduino] Using local Arduino stubs for dependencies..."

# Limpieza total
find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true
find "${BUILD_DIR}" -name '*.gcno' -delete 2>/dev/null || true
find "${BUILD_DIR}" -name '*.o' -delete 2>/dev/null || true

SOURCES=(
    "${SRC_ROOT}/protocol/rpc_frame.cpp"
    "${SRC_ROOT}/security/security.cpp"
    "${SRC_ROOT}/services/Bridge.cpp"
    "${SRC_ROOT}/services/Console.cpp"
    "${SRC_ROOT}/services/DataStore.cpp"
    "${SRC_ROOT}/services/Process.cpp"
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

TEST_FILES=(
    "${TEST_ROOT}/test_integrated.cpp"
    "${TEST_ROOT}/test_bridge_core.cpp"
    "${TEST_ROOT}/test_bridge_components.cpp"
    "${TEST_ROOT}/test_protocol.cpp"
    "${TEST_ROOT}/test_extreme_coverage.cpp"
    "${TEST_ROOT}/test_extreme_coverage_v2.cpp"
    "${TEST_ROOT}/test_arduino_100_coverage.cpp"
    "${TEST_ROOT}/test_coverage_100_final.cpp"
)

echo "[coverage_arduino] Compilando y ejecutando suites secuencialmente..."
for test_file in "${TEST_FILES[@]}"; do
    test_name=$(basename "${test_file}" .cpp)
    echo "  -> Procesando ${test_name}..."
    g++ "${COMPILE_FLAGS[@]}" "${SOURCES[@]}" "${test_file}" -o "${BUILD_DIR}/${test_name}"
    "${BUILD_DIR}/${test_name}"
done

echo "[coverage_arduino] Generando informes..."
GCOVR_EXCLUDES=(
    --exclude "${SRC_ROOT}/etl"
)

gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" "${GCOVR_EXCLUDES[@]}" --print-summary >"${OUTPUT_ROOT}/summary.txt"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" "${GCOVR_EXCLUDES[@]}" --json-summary "${OUTPUT_ROOT}/summary.json"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" "${GCOVR_EXCLUDES[@]}" --xml "${OUTPUT_ROOT}/coverage.xml"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" "${GCOVR_EXCLUDES[@]}" --html-details "${OUTPUT_ROOT}/index.html"

echo "[coverage_arduino] Reporte finalizado en ${OUTPUT_ROOT}"
