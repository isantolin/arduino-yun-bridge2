#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LIB_DIR="${ROOT_DIR}/openwrt-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"

BUILD_DIR="${LIB_DIR}/build-host-local"
mkdir -p "${BUILD_DIR}"

usage() {
  cat <<'EOF'
Usage: tools/ci_arduino_host_tests.sh [--cxx COMPILER]

Options:
  --cxx COMPILER  C++ compiler to use (default: g++)
  -h, --help      Show this help
EOF
}

CXX="g++"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cxx)
      CXX="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[host-cpp] Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

COMMON_DEFS=(
  -DBRIDGE_HOST_TEST=1
)

ARDUINO_TEST_DEFS=(
  "${COMMON_DEFS[@]}"
  -DBRIDGE_TEST_NO_GLOBALS=1
)

COMMON_FLAGS=(
  -std=c++11
  -O0
  -g
)

# [CI/CD] Dependency Management
DEP_DIR="${BUILD_DIR}/deps"
mkdir -p "${DEP_DIR}"

ensure_dep() {
  local name="$1"
  local url="$2"
  local target="${DEP_DIR}/${name}"
  if [ ! -d "${target}" ]; then
    echo "[host-cpp] Downloading ${name}..."
    local tmp
    tmp=$(mktemp -d)
    curl -fsSL "${url}" | tar xz -C "${tmp}" --strip-components=1
    mv "${tmp}" "${target}"
  fi
}

ensure_dep "etl" "https://github.com/ETLCPP/etl/archive/refs/tags/20.39.4.tar.gz"
ensure_dep "taskscheduler" "https://github.com/arkhipenko/TaskScheduler/archive/refs/tags/v3.8.5.tar.gz"
ensure_dep "fastcrc" "https://github.com/FrankBoesing/FastCRC/archive/refs/heads/master.tar.gz"
ensure_dep "packetserial" "https://github.com/bakercp/PacketSerial/archive/refs/heads/master.tar.gz"

COMMON_INCLUDES=(
  -I"${SRC_DIR}"
  -I"${TEST_DIR}/mocks"
  -I"${STUB_DIR}"
  -I"${DEP_DIR}/etl/include"
  -I"${DEP_DIR}/taskscheduler/src"
  -I"${DEP_DIR}/fastcrc/src"
  -I"${DEP_DIR}/packetserial/src"
)

PROTOCOL_SOURCES=(
  "${SRC_DIR}/protocol/rpc_frame.cpp"
)

ARDUINO_RUNTIME_SOURCES=(
  "${SRC_DIR}/arduino/Bridge.cpp"
  "${SRC_DIR}/arduino/BridgeTransport.cpp"
  "${SRC_DIR}/arduino/Console.cpp"
  "${SRC_DIR}/arduino/DataStore.cpp"
  "${SRC_DIR}/arduino/FileSystem.cpp"
  "${SRC_DIR}/arduino/Mailbox.cpp"
  "${SRC_DIR}/arduino/Process.cpp"
  "${PROTOCOL_SOURCES[@]}"
)

build_one() {
  local test_cpp="$1"
  local out_bin="$2"
  shift 2
  local -a extra_defs=("$@")

  echo "[host-cpp] Building ${out_bin##*/}"
  "${CXX}" \
    "${COMMON_FLAGS[@]}" \
    "${COMMON_DEFS[@]}" \
    "${extra_defs[@]}" \
    "${COMMON_INCLUDES[@]}" \
    "${test_cpp}" \
    -o "${out_bin}"
}

build_protocol() {
  local test_cpp="$1"
  local out_bin="$2"

  echo "[host-cpp] Building ${out_bin##*/}"
  "${CXX}" \
    "${COMMON_FLAGS[@]}" \
    "${COMMON_INCLUDES[@]}" \
    "${test_cpp}" \
    "${PROTOCOL_SOURCES[@]}" \
    -o "${out_bin}"
}

run_one() {
  local bin="$1"
  echo "[host-cpp] Running ${bin##*/}"
  "${bin}"
}

build_protocol "${TEST_DIR}/test_protocol.cpp" "${BUILD_DIR}/test_protocol"

echo "[host-cpp] Building test_bridge_components"
"${CXX}" "${COMMON_FLAGS[@]}" "${ARDUINO_TEST_DEFS[@]}" "${COMMON_INCLUDES[@]}" \
  "${TEST_DIR}/test_bridge_components.cpp" \
  "${ARDUINO_RUNTIME_SOURCES[@]}" \
  -o "${BUILD_DIR}/test_bridge_components"

echo "[host-cpp] Building test_bridge_core"
"${CXX}" "${COMMON_FLAGS[@]}" "${ARDUINO_TEST_DEFS[@]}" "${COMMON_INCLUDES[@]}" \
  "${TEST_DIR}/test_bridge_core.cpp" \
  "${ARDUINO_RUNTIME_SOURCES[@]}" \
  -o "${BUILD_DIR}/test_bridge_core"

echo "[host-cpp] Building test_coverage_extreme"
"${CXX}" "${COMMON_FLAGS[@]}" "${ARDUINO_TEST_DEFS[@]}" "${COMMON_INCLUDES[@]}" \
  "${TEST_DIR}/test_coverage_extreme.cpp" \
  "${ARDUINO_RUNTIME_SOURCES[@]}" \
  -o "${BUILD_DIR}/test_coverage_extreme"

echo "[host-cpp] Building test_coverage_gaps"
"${CXX}" "${COMMON_FLAGS[@]}" "${ARDUINO_TEST_DEFS[@]}" "${COMMON_INCLUDES[@]}" \
  "${TEST_DIR}/test_coverage_gaps.cpp" \
  "${ARDUINO_RUNTIME_SOURCES[@]}" \
  -o "${BUILD_DIR}/test_coverage_gaps"

run_one "${BUILD_DIR}/test_protocol"
run_one "${BUILD_DIR}/test_bridge_components"
run_one "${BUILD_DIR}/test_bridge_core"
run_one "${BUILD_DIR}/test_coverage_extreme"
run_one "${BUILD_DIR}/test_coverage_gaps"

echo "[host-cpp] ALL HOST TESTS PASSED"
