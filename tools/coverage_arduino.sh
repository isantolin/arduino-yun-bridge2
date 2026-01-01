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

PROTOCOL_SOURCES=(
  "${SRC_ROOT}/protocol/crc.cpp"
  "${SRC_ROOT}/protocol/rpc_frame.cpp"
)

ARDUINO_RUNTIME_SOURCES=(
  "${SRC_ROOT}/arduino/Bridge.cpp"
  "${SRC_ROOT}/arduino/BridgeTransport.cpp"
  "${SRC_ROOT}/arduino/Console.cpp"
  "${SRC_ROOT}/arduino/DataStore.cpp"
  "${SRC_ROOT}/arduino/Mailbox.cpp"
  "${SRC_ROOT}/arduino/FileSystem.cpp"
  "${SRC_ROOT}/arduino/Process.cpp"
  "${PROTOCOL_SOURCES[@]}"
)

COMPILE_FLAGS=(
  -std=c++11      # Updated to C++11 as requested
  -g              # Debug symbols enabled
  -O0             # Disable optimizations
  -Wall
  -Wextra
  -pedantic
  -DBRIDGE_HOST_TEST=1
  -DBRIDGE_TEST_NO_GLOBALS=1
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

# Always clean up old coverage data to prevent stale references
find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true
# .gcno files are generated during compilation, so we should ONLY delete them if we are rebuilding.
# find "${BUILD_DIR}" -name '*.gcno' -delete 2>/dev/null || true

if [[ ! -x "${BUILD_DIR}/protocol/test_protocol" || ! -x "${BUILD_DIR}/components/test_bridge_components" || ! -x "${BUILD_DIR}/core/test_bridge_core" || ! -x "${BUILD_DIR}/extreme/test_coverage_extreme" || ! -x "${BUILD_DIR}/transport/test_bridge_transport" ]]; then
  RUN_BUILD=1
fi

build_one() {
  local suite_dir="$1"
  local out_bin="$2"
  shift 2
  local -a sources=("$@")

  mkdir -p "${suite_dir}"

  local -a objects=()
  local src
  for src in "${sources[@]}"; do
    local base
    base="$(basename "${src}")"
    local obj="${suite_dir}/${base%.cpp}.o"
    echo "[coverage_arduino]  g++ -c ${base} -> ${obj##*/}" >&2
    g++ "${COMPILE_FLAGS[@]}" -c "${src}" -o "${obj}"
    objects+=("${obj}")
  done

  echo "[coverage_arduino]  g++ (link) ${out_bin##*/}" >&2
  g++ "${COMPILE_FLAGS[@]}" "${objects[@]}" -o "${out_bin}"
}

if [[ ${RUN_BUILD} -eq 1 ]]; then
  echo "[coverage_arduino] Compilando harness de protocolo con flags de cobertura..." >&2
  # Cleanup is already done above, but we keep this for safety if logic changes
  find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true
  find "${BUILD_DIR}" -name '*.gcno' -delete 2>/dev/null || true

  echo "[coverage_arduino] Compilando test_protocol" >&2
  build_one \
    "${BUILD_DIR}/protocol" \
    "${BUILD_DIR}/protocol/test_protocol" \
    "${PROTOCOL_SOURCES[@]}" \
    "${TEST_ROOT}/test_protocol.cpp"

  echo "[coverage_arduino] Compilando test_bridge_components" >&2
  build_one \
    "${BUILD_DIR}/components" \
    "${BUILD_DIR}/components/test_bridge_components" \
    "${ARDUINO_RUNTIME_SOURCES[@]}" \
    "${TEST_ROOT}/test_bridge_components.cpp"

  echo "[coverage_arduino] Compilando test_bridge_core" >&2
  build_one \
    "${BUILD_DIR}/core" \
    "${BUILD_DIR}/core/test_bridge_core" \
    "${ARDUINO_RUNTIME_SOURCES[@]}" \
    "${TEST_ROOT}/test_bridge_core.cpp"

  echo "[coverage_arduino] Compilando test_coverage_extreme" >&2
  build_one \
    "${BUILD_DIR}/extreme" \
    "${BUILD_DIR}/extreme/test_coverage_extreme" \
    "${ARDUINO_RUNTIME_SOURCES[@]}" \
    "${TEST_ROOT}/test_coverage_extreme.cpp"

  echo "[coverage_arduino] Compilando test_bridge_transport" >&2
  build_one \
    "${BUILD_DIR}/transport" \
    "${BUILD_DIR}/transport/test_bridge_transport" \
    "${ARDUINO_RUNTIME_SOURCES[@]}" \
    "${TEST_ROOT}/test_bridge_transport.cpp"
fi

echo "[coverage_arduino] Ejecutando tests host..." >&2
ls -l "${BUILD_DIR}/components/test_bridge_components"
ls -l "${TEST_ROOT}/test_bridge_components.cpp"
"${BUILD_DIR}/protocol/test_protocol"
"${BUILD_DIR}/components/test_bridge_components"
"${BUILD_DIR}/core/test_bridge_core"
"${BUILD_DIR}/extreme/test_coverage_extreme"
"${BUILD_DIR}/transport/test_bridge_transport"

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

if [[ "${ARDUINO_COVERAGE_HTML:-1}" -eq 1 ]]; then
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
fi

echo "[coverage_arduino] Reporte generado en:" >&2
echo "  - ${SUMMARY_PATH}" >&2
echo "  - ${XML_PATH}" >&2
if [[ "${ARDUINO_COVERAGE_HTML:-1}" -eq 1 ]]; then
  echo "  - ${HTML_PATH}" >&2
  echo "  - ${BRIDGE_HTML_PATH}" >&2
  echo "  - ${CONSOLE_HTML_PATH}" >&2
fi
