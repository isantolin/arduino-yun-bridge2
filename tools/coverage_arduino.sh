#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/openwrt-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"

BUILD_DIR="${LIB_ROOT}/build-coverage"
OUTPUT_ROOT="${ROOT_ROOT:-${ROOT_DIR}}/coverage/arduino"
JSON_DIR="${OUTPUT_ROOT}/json"
mkdir -p "${BUILD_DIR}" "${JSON_DIR}"

# [SIL-2] Ensure dependencies are present
echo "[coverage_arduino] Installing library dependencies..."
DUMMY_ARDUINO_LIBS=$(mktemp -d)
"${LIB_ROOT}/tools/install.sh" "${DUMMY_ARDUINO_LIBS}"

SOURCES=(
    "${SRC_ROOT}/security/security.cpp"
    "${SRC_ROOT}/hal/hal.cpp"
    "${SRC_ROOT}/Bridge.cpp"
    "${SRC_ROOT}/services/Console.cpp"
    "${SRC_ROOT}/services/DataStore.cpp"
    "${SRC_ROOT}/services/Mailbox.cpp"
    "${SRC_ROOT}/services/FileSystem.cpp"
    "${SRC_ROOT}/services/Process.cpp"
)

# Flag crítica para evitar colisiones de Bridge/Console
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
    -DNUM_DIGITAL_PINS=20
    -I"${SRC_ROOT}"
    -I"${TEST_ROOT}"
    -I"${TEST_ROOT}/mocks"
    -I"${STUB_INCLUDE}"
    -I"${DUMMY_ARDUINO_LIBS}/Crypto"
    -I"${DUMMY_ARDUINO_LIBS}/PacketSerial"
)

echo "[coverage_arduino] Compilando objetos base..."
OBJECTS=()
for src in "${SOURCES[@]}"; do
    obj_base=$(basename "${src}" .cpp)
    obj="${BUILD_DIR}/${obj_base}.o"
    g++ "${COMPILE_FLAGS[@]}" -c "${src}" -o "${obj}"
    OBJECTS+=("${obj}")
done

TEST_FILES=(
    "${TEST_ROOT}/test_integrated.cpp"
    "${TEST_ROOT}/test_bridge_core.cpp"
    "${TEST_ROOT}/test_bridge_components.cpp"
    "${TEST_ROOT}/test_protocol.cpp"
    "${TEST_ROOT}/test_extreme_coverage.cpp"
    "${TEST_ROOT}/test_extreme_coverage_v2.cpp"
    "${TEST_ROOT}/test_arduino_100_coverage.cpp"
    "${TEST_ROOT}/test_coverage_100_final.cpp"
    "${TEST_ROOT}/test_arduino_coverage_boost.cpp"
    "${TEST_ROOT}/test_coverage_final_push.cpp"
)

echo "[coverage_arduino] Ejecutando suites..."
for test_file in "${TEST_FILES[@]}"; do
    test_name=$(basename "${test_file}" .cpp)
    echo "  -> Suite: ${test_name}"
    
    # Limpiamos solo los .gcda de los objetos para que cada test aporte su parte
    find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true
    
    # Compilamos el test y enlazamos
    g++ "${COMPILE_FLAGS[@]}" "${test_file}" "${OBJECTS[@]}" -o "${BUILD_DIR}/${test_name}"
    
    # Ejecutamos SIN silenciar
    "${BUILD_DIR}/${test_name}"
    
    # Capturamos la cobertura de esta ejecución en JSON
    gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --exclude "${SRC_ROOT}/etl" --json "${JSON_DIR}/${test_name}.json"
done

echo "[coverage_arduino] Consolidando reporte final..."
gcovr --root "${SRC_ROOT}" --add-tracefile "${JSON_DIR}/*.json" --filter "${SRC_ROOT}" --print-summary >"${OUTPUT_ROOT}/summary.txt"
gcovr --root "${SRC_ROOT}" --add-tracefile "${JSON_DIR}/*.json" --filter "${SRC_ROOT}" --json-summary "${OUTPUT_ROOT}/summary.json"
gcovr --root "${SRC_ROOT}" --add-tracefile "${JSON_DIR}/*.json" --filter "${SRC_ROOT}" --xml "${OUTPUT_ROOT}/coverage.xml"
gcovr --root "${SRC_ROOT}" --add-tracefile "${JSON_DIR}/*.json" --filter "${SRC_ROOT}" --html-details "${OUTPUT_ROOT}/index.html"

cat "${OUTPUT_ROOT}/summary.txt"
echo "[coverage_arduino] Proceso finalizado."
