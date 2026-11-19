#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"
SRC_DIR="${LIB_DIR}/src"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"
METADATA_DIR="${ROOT_DIR}/build/clang-tidy"
ARDUINO_LIBS="${ARDUINO_LIB_DIR:-$HOME/Arduino/libraries}"

python3 "${ROOT_DIR}/tools/arduino_core_metadata.py" \
    --host-test \
    --write-header "${METADATA_DIR}/ArduinoCoreMetadata.h"

# Ensure Arduino libraries are present
if [ ! -d "$ARDUINO_LIBS/Embedded_Template_Library" ] || \
   [ ! -f "$ARDUINO_LIBS/wolfssl/src/user_settings.h" ] || \
   [ ! -f "$SRC_DIR/pb_common.h" ] || \
   [ ! -f "$SRC_DIR/pb_decode.h" ] || \
   [ ! -f "$SRC_DIR/pb_encode.h" ]; then
    mkdir -p "${ARDUINO_LIBS}"
    "${LIB_DIR}/tools/install.sh" "${ARDUINO_LIBS}"
fi

# shellcheck source=tools/ci/arduino_toolchains.sh
source "${ROOT_DIR}/tools/ci/arduino_toolchains.sh"

ETL_INC="$ARDUINO_LIBS/Embedded_Template_Library/src"
WOLFSSL_INC="$ARDUINO_LIBS/wolfssl/src"

INCLUDES=(
    "-I${SRC_DIR}"
    "-I${SRC_DIR}/config"
    "-I${SRC_DIR}/protocol"
    "-I${METADATA_DIR}"
    "-I${STUB_DIR}"
    "-I${PW_ROOT}/pw_sync/public_overrides"
    "-I${PW_ROOT}/pw_sync_stl/public_overrides"
    "-I${PW_ROOT}/pw_sync_stl/public"
    "-isystem${ETL_INC}"
    "-isystem${WOLFSSL_INC}"
    "-isystem${ARDUINO_LIBS}/wolfssl"
    "${PW_INC_FLAGS[@]}"
)

COMPILE_FLAGS=(
    "-std=c++17"
    "-DPROGMEM="
    "-DPB_PROTO_HEADER_VERSION=40"
    "-DETL_NO_STL"
    "-DETL_FORCE_STD_INITIALIZER_LIST"
    "-DWOLFSSL_USER_SETTINGS"
    "-DPW_RPC_USE_GLOBAL_MUTEX=0"
    "${INCLUDES[@]}"
)

echo "[clang-tidy] Auditing C++ sources with clang-tidy..."
SOURCES=($(find "${SRC_DIR}" -name "*.cpp" ! -name "pb_*.c" ! -name "*.pb.c"))

for src in "${SOURCES[@]}"; do
    echo "[clang-tidy] Checking $(basename "$src")..."
    clang-tidy "$src" -- "${COMPILE_FLAGS[@]}"
done

echo "[clang-tidy] All C++ sources passed static analysis successfully."
