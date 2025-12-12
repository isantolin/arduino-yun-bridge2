#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/openwrt-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"
BUILD_DIR="${BUILD_DIR:-${LIB_ROOT}/build-coverage}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${ROOT_DIR}/coverage/arduino}"
SUMMARY_JSON_PATH="${OUTPUT_ROOT}/summary.json"

COMPILE_FLAGS=(
  -std=c++17
  -Wall
  -Wextra
  -pedantic
  -DBRIDGE_HOST_TEST=1
  -DBRIDGE_SERIAL_SHARED_SECRET=\"host_test_secret\"
  -DBRIDGE_FIRMWARE_VERSION_MAJOR=2
  -DBRIDGE_FIRMWARE_VERSION_MINOR=0
  -fprofile-arcs
  -ftest-coverage
  -I"${SRC_ROOT}"
  -I"${STUB_INCLUDE}"
)

if ! command -v gcovr >/dev/null 2>&1; then
  echo "[coverage_arduino] Instala gcovr (pip install gcovr) antes de ejecutar este script." >&2
  exit 1
fi

if ! command -v g++ >/dev/null 2>&1; then
  echo "[coverage_arduino] Se requiere g++ para compilar los tests host." >&2
  exit 1
fi

mkdir -p "${BUILD_DIR}"

RUN_BUILD=0
if [[ "${FORCE_REBUILD:-0}" -eq 1 ]]; then
  RUN_BUILD=1
fi

if [[ ! -x "${BUILD_DIR}/test_protocol" || ! -x "${BUILD_DIR}/test_bridge_components" ]]; then
  RUN_BUILD=1
fi

if [[ ${RUN_BUILD} -eq 1 ]]; then
  echo "[coverage_arduino] Compilando harness de protocolo con flags de cobertura..." >&2
  find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true
  find "${BUILD_DIR}" -name '*.gcno' -delete 2>/dev/null || true

  g++ "${COMPILE_FLAGS[@]}" -c "${SRC_ROOT}/protocol/crc.cpp" -o "${BUILD_DIR}/crc.o"
  g++ "${COMPILE_FLAGS[@]}" -c "${SRC_ROOT}/protocol/rpc_frame.cpp" -o "${BUILD_DIR}/rpc_frame.o"
  g++ "${COMPILE_FLAGS[@]}" -c "${SRC_ROOT}/arduino/Bridge.cpp" -o "${BUILD_DIR}/Bridge.o"
  g++ "${COMPILE_FLAGS[@]}" -c "${SRC_ROOT}/arduino/Console.cpp" -o "${BUILD_DIR}/Console.o"
  g++ "${COMPILE_FLAGS[@]}" -c "${SRC_ROOT}/arduino/Peripherals.cpp" -o "${BUILD_DIR}/Peripherals.o"
  g++ "${COMPILE_FLAGS[@]}" -c "${TEST_ROOT}/test_protocol.cpp" -o "${BUILD_DIR}/test_protocol.o"
  g++ "${COMPILE_FLAGS[@]}" -c "${TEST_ROOT}/test_bridge_components.cpp" -o "${BUILD_DIR}/test_bridge_components.o"

  g++ "${COMPILE_FLAGS[@]}" \
    "${BUILD_DIR}/crc.o" \
    "${BUILD_DIR}/rpc_frame.o" \
    "${BUILD_DIR}/test_protocol.o" \
    -o "${BUILD_DIR}/test_protocol"

  g++ "${COMPILE_FLAGS[@]}" \
    "${BUILD_DIR}/crc.o" \
    "${BUILD_DIR}/rpc_frame.o" \
    "${BUILD_DIR}/Bridge.o" \
    "${BUILD_DIR}/Console.o" \
    "${BUILD_DIR}/Peripherals.o" \
    "${BUILD_DIR}/test_bridge_components.o" \
    -o "${BUILD_DIR}/test_bridge_components"
fi

echo "[coverage_arduino] Ejecutando tests host..." >&2
ls -l "${BUILD_DIR}/test_bridge_components"
ls -l "${TEST_ROOT}/test_bridge_components.cpp"
"${BUILD_DIR}/test_protocol"
"${BUILD_DIR}/test_bridge_components"

shopt -s nullglob globstar
GCDA_FILES=(${BUILD_DIR}/**/*.gcda)
shopt -u nullglob globstar

if [[ ${#GCDA_FILES[@]} -eq 0 ]]; then
  echo "[coverage_arduino] No se encontraron archivos .gcda en '${BUILD_DIR}'." >&2
  echo "  Asegúrate de que el harness se ejecutó correctamente y que se compilaron fuentes con flags de cobertura." >&2
  exit 3
fi

mkdir -p "${OUTPUT_ROOT}"

SUMMARY_PATH="${OUTPUT_ROOT}/summary.txt"
HTML_PATH="${OUTPUT_ROOT}/index.html"
XML_PATH="${OUTPUT_ROOT}/coverage.xml"
BRIDGE_HTML_PATH="${OUTPUT_ROOT}/bridge_handshake.html"
CONSOLE_HTML_PATH="${OUTPUT_ROOT}/console_flow.html"

gcovr \
  --root "${SRC_ROOT}" \
  --object-directory "${BUILD_DIR}" \
  --filter "${SRC_ROOT}" \
  --print-summary >"${SUMMARY_PATH}"

gcovr \
  --root "${SRC_ROOT}" \
  --object-directory "${BUILD_DIR}" \
  --filter "${SRC_ROOT}" \
  --json-summary "${SUMMARY_JSON_PATH}"

gcovr \
  --root "${SRC_ROOT}" \
  --object-directory "${BUILD_DIR}" \
  --filter "${SRC_ROOT}" \
  --xml "${XML_PATH}"

gcovr \
  --root "${SRC_ROOT}" \
  --object-directory "${BUILD_DIR}" \
  --filter "${SRC_ROOT}" \
  --html-details "${HTML_PATH}"

gcovr \
  --root "${SRC_ROOT}" \
  --object-directory "${BUILD_DIR}" \
  --filter "${SRC_ROOT}/arduino/Bridge.cpp" \
  --html-details "${BRIDGE_HTML_PATH}"

gcovr \
  --root "${SRC_ROOT}" \
  --object-directory "${BUILD_DIR}" \
  --filter "${SRC_ROOT}/arduino/Console.cpp" \
  --html-details "${CONSOLE_HTML_PATH}"

echo "[coverage_arduino] Reporte generado en:" >&2
echo "  - ${SUMMARY_PATH}" >&2
echo "  - ${XML_PATH}" >&2
echo "  - ${HTML_PATH}" >&2
echo "  - ${BRIDGE_HTML_PATH}" >&2
echo "  - ${CONSOLE_HTML_PATH}" >&2