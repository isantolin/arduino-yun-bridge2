#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB_ROOT="${ROOT_DIR}/openwrt-library-arduino"
SRC_ROOT="${LIB_ROOT}/src"
TEST_ROOT="${LIB_ROOT}/tests"
STUB_INCLUDE="${ROOT_DIR}/tools/arduino_stub/include"

usage() {
  cat <<'EOF'
Usage: tools/coverage_arduino.sh [--build-dir DIR] [--output-root DIR] [--force-rebuild] [--no-html]

Options:
  --build-dir DIR     Directory for build outputs (default: openwrt-library-arduino/build-coverage)
  --output-root DIR   Directory for reports (default: coverage/arduino)
  --force-rebuild     Rebuild harness even if binaries exist
  --no-html           Disable HTML reports
  -h, --help          Show this help
EOF
}

BUILD_DIR="${LIB_ROOT}/build-coverage"
OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
FORCE_REBUILD=0
ENABLE_HTML=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-dir)
      BUILD_DIR="$2"
      shift 2
      ;;
    --output-root)
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    --force-rebuild)
      FORCE_REBUILD=1
      shift
      ;;
    --no-html)
      ENABLE_HTML=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[coverage_arduino] Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

SUMMARY_JSON_PATH="${OUTPUT_ROOT}/summary.json"

PROTOCOL_SOURCES=(
  "${SRC_ROOT}/protocol/rpc_frame.cpp"
)

ARDUINO_RUNTIME_SOURCES=(
  "${SRC_ROOT}/arduino/Bridge.cpp"
  "${SRC_ROOT}/arduino/Console.cpp"
  "${SRC_ROOT}/arduino/DataStore.cpp"
  "${SRC_ROOT}/arduino/Mailbox.cpp"
  "${SRC_ROOT}/arduino/FileSystem.cpp"
  "${SRC_ROOT}/arduino/Process.cpp"
  "${PROTOCOL_SOURCES[@]}"
)

TEST_SOURCES=(
  "${TEST_ROOT}/test_protocol.cpp"
  "${TEST_ROOT}/test_bridge_components.cpp"
  "${TEST_ROOT}/test_bridge_core.cpp"
  "${TEST_ROOT}/test_coverage_extreme.cpp"
  "${TEST_ROOT}/test_coverage_gaps.cpp"
)

COMPILE_FLAGS=(
  -std=c++11
  -g
  -O0
  -Wall
  -Wextra
  -pedantic
  -DBRIDGE_HOST_TEST=1
  -DBRIDGE_TEST_NO_GLOBALS=1
  -DBRIDGE_SERIAL_SHARED_SECRET="host_test_secret"
  -DBRIDGE_FIRMWARE_VERSION_MAJOR=2
  -DBRIDGE_FIRMWARE_VERSION_MINOR=0
  -fprofile-arcs
  -ftest-coverage
  -I"${SRC_ROOT}"
  -I"${TEST_ROOT}/mocks"
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

if [ ! -d "${SRC_ROOT}/etl" ]; then
  echo "[coverage_arduino] Missing ETL dependency. Running installer..."
  "${LIB_ROOT}/tools/install.sh"
fi

BUILD_SIGNATURE_PATH="${BUILD_DIR}/.coverage_build_signature"

RUN_BUILD=0
if [[ "${FORCE_REBUILD}" -eq 1 ]]; then
  RUN_BUILD=1
fi

hash_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${path}" | awk '{print $1}'
    return
  fi
  python3 - "${path}" <<'PY'
import hashlib
import sys
path = sys.argv[1]
h = hashlib.sha256()
with open(path, 'rb') as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b''):
        h.update(chunk)
print(h.hexdigest())
PY
}

compute_build_signature() {
  local tmp
  tmp="$(mktemp)"
  {
    echo "gcovr=$(gcovr --version 2>/dev/null | head -n 1 || true)"
    echo "g++=$(g++ --version 2>/dev/null | head -n 1 || true)"
    echo "-- compile flags --"
    printf '%s\n' "${COMPILE_FLAGS[@]}"
    echo "-- sources --"
    local src
    for src in "${ARDUINO_RUNTIME_SOURCES[@]}" "${TEST_SOURCES[@]}"; do
      echo "${src}:$(hash_file "${src}")"
    done
  } >"${tmp}"
  sha256sum "${tmp}" | awk '{print $1}'
  rm -f "${tmp}"
}

find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true

if [[ ! -x "${BUILD_DIR}/protocol/test_protocol" || ! -x "${BUILD_DIR}/components/test_bridge_components" || ! -x "${BUILD_DIR}/core/test_bridge_core" || ! -x "${BUILD_DIR}/extreme/test_coverage_extreme" || ! -x "${BUILD_DIR}/gaps/test_coverage_gaps" ]]; then
  RUN_BUILD=1
fi

if [[ "${RUN_BUILD}" -eq 0 ]]; then
  if [[ ! -f "${BUILD_SIGNATURE_PATH}" ]]; then
    RUN_BUILD=1
  else
    CURRENT_SIG="$(compute_build_signature)"
    STORED_SIG="$(cat "${BUILD_SIGNATURE_PATH}")"
    if [[ "${CURRENT_SIG}" != "${STORED_SIG}" ]]; then
      RUN_BUILD=1
    fi
  fi
fi

build_one() {
  local suite_dir="$1"
  local out_bin="$2"
  shift 2
  local -a sources=($@)
  mkdir -p "${suite_dir}"
  local -a objects=()
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
  echo "[coverage_arduino] Compilando harness con flags de cobertura..." >&2
  find "${BUILD_DIR}" -name '*.gcda' -delete 2>/dev/null || true
  find "${BUILD_DIR}" -name '*.gcno' -delete 2>/dev/null || true
  find "${BUILD_DIR}" -name '*.o' -delete 2>/dev/null || true

  build_one "${BUILD_DIR}/protocol" "${BUILD_DIR}/protocol/test_protocol" "${PROTOCOL_SOURCES[@]}" "${TEST_ROOT}/test_protocol.cpp"
  build_one "${BUILD_DIR}/components" "${BUILD_DIR}/components/test_bridge_components" "${ARDUINO_RUNTIME_SOURCES[@]}" "${TEST_ROOT}/test_bridge_components.cpp"
  build_one "${BUILD_DIR}/core" "${BUILD_DIR}/core/test_bridge_core" "${ARDUINO_RUNTIME_SOURCES[@]}" "${TEST_ROOT}/test_bridge_core.cpp"
  build_one "${BUILD_DIR}/extreme" "${BUILD_DIR}/extreme/test_coverage_extreme" "${ARDUINO_RUNTIME_SOURCES[@]}" "${TEST_ROOT}/test_coverage_extreme.cpp"
  build_one "${BUILD_DIR}/gaps" "${BUILD_DIR}/gaps/test_coverage_gaps" "${ARDUINO_RUNTIME_SOURCES[@]}" "${TEST_ROOT}/test_coverage_gaps.cpp"

  compute_build_signature >"${BUILD_SIGNATURE_PATH}"
fi

echo "[coverage_arduino] Ejecutando tests..." >&2
"${BUILD_DIR}/protocol/test_protocol"
"${BUILD_DIR}/components/test_bridge_components"
"${BUILD_DIR}/core/test_bridge_core"
"${BUILD_DIR}/extreme/test_coverage_extreme"
"${BUILD_DIR}/gaps/test_coverage_gaps"

shopt -s nullglob globstar
GCDA_FILES=(${BUILD_DIR}/**/*.gcda)
shopt -u nullglob globstar

if [[ ${#GCDA_FILES[@]} -eq 0 ]]; then
  echo "[coverage_arduino] No se encontraron archivos .gcda." >&2
  exit 3
fi

mkdir -p "${OUTPUT_ROOT}"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --print-summary >"${OUTPUT_ROOT}/summary.txt"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --json-summary "${SUMMARY_JSON_PATH}"
gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --xml "${OUTPUT_ROOT}/coverage.xml"

if [[ "${ENABLE_HTML}" -eq 1 ]]; then
  gcovr --root "${SRC_ROOT}" --object-directory "${BUILD_DIR}" --filter "${SRC_ROOT}" --html-details "${OUTPUT_ROOT}/index.html"
fi

echo "[coverage_arduino] Reporte generado en ${OUTPUT_ROOT}" >&2
