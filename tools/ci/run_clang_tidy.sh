#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"
SRC_DIR="${LIB_DIR}/src"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"
ARDUINO_LIBS="${ARDUINO_LIB_DIR:-$HOME/Arduino/libraries}"

# Ensure Arduino libraries are present
if [ ! -d "$ARDUINO_LIBS/Embedded_Template_Library" ]; then
    mkdir -p "${ARDUINO_LIBS}"
    "${LIB_DIR}/tools/install.sh" "${ARDUINO_LIBS}"
fi

ETL_INC="$ARDUINO_LIBS/Embedded_Template_Library/src"
WOLFSSL_DIR="$ARDUINO_LIBS/wolfSSL"
if [ ! -d "$WOLFSSL_DIR" ]; then WOLFSSL_DIR="$ARDUINO_LIBS/wolfssl"; fi
PACKETSERIAL_INC="$ARDUINO_LIBS/PacketSerial/src"

INCLUDES=(
    "-I${SRC_DIR}"
    "-I${SRC_DIR}/config"
    "-I${SRC_DIR}/protocol"
    "-I${STUB_DIR}"
    "-I${ETL_INC}"
    "-I${WOLFSSL_DIR}"
    "-I${WOLFSSL_DIR}/src"
    "-I${PACKETSERIAL_INC}"
)

COMPILE_FLAGS=(
    "-std=c++17"
    "-DPROGMEM="
    "-DPB_PROTO_HEADER_VERSION=40"
    "-DWOLFSSL_USER_SETTINGS"
    "-DETL_NO_STL"
    "${INCLUDES[@]}"
)

echo "[clang-tidy] Auditing C++ sources with clang-tidy..."
SOURCES=($(find "${SRC_DIR}" -name "*.cpp" ! -name "pb_*.c" ! -name "*.pb.c"))

for src in "${SOURCES[@]}"; do
    echo "[clang-tidy] Checking $(basename "$src")..."
    clang-tidy "$src" -- "${COMPILE_FLAGS[@]}"
done

echo "[clang-tidy] All C++ sources passed static analysis successfully."
