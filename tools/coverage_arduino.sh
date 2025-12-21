#!/usr/bin/env bash
set -euo pipefail

# [HARDENING] Definición robusta de directorios
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$ROOT_DIR")"
ARDUINO_LIB_DIR="${PROJECT_ROOT}/openwrt-library-arduino"
BUILD_DIR="${ARDUINO_LIB_DIR}/build-coverage"
SRC_DIR="${ARDUINO_LIB_DIR}/src"
TEST_DIR="${ARDUINO_LIB_DIR}/tests"

# [CRITICAL] Limpieza proactiva para evitar "stamp mismatch" en gcov
echo "[coverage_arduino] Limpiando artefactos previos..."
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

echo "[coverage_arduino] Compilando harness de protocolo con flags de cobertura..."

# Compilación con g++ (Host) inyectando flags de cobertura
# Se excluyen mocks y tests del análisis, enfocándose en src/
g++ -O0 -g --coverage \
    -std=c++11 \
    -DARDUINO=100 \
    -DBRIDGE_HOST_TEST=1 \
    -I"${ARDUINO_LIB_DIR}/tools/arduino_stub/include" \
    -I"${SRC_DIR}" \
    -I"${TEST_DIR}/mocks" \
    "${SRC_DIR}/arduino/Bridge.cpp" \
    "${SRC_DIR}/arduino/BridgeTransport.cpp" \
    "${SRC_DIR}/arduino/Console.cpp" \
    "${SRC_DIR}/arduino/DataStore.cpp" \
    "${SRC_DIR}/arduino/FileSystem.cpp" \
    "${SRC_DIR}/arduino/Mailbox.cpp" \
    "${SRC_DIR}/arduino/Process.cpp" \
    "${SRC_DIR}/protocol/crc.cpp" \
    "${SRC_DIR}/protocol/rpc_frame.cpp" \
    "${TEST_DIR}/test_bridge_components.cpp" \
    -o "${BUILD_DIR}/test_bridge_components"

echo "[coverage_arduino] Ejecutando tests host..."
"${BUILD_DIR}/test_bridge_components"

echo "[coverage_arduino] Generando reporte de cobertura..."

# Generación del reporte filtrando solo el código fuente relevante
gcovr \
    --root "${PROJECT_ROOT}" \
    --filter "${SRC_DIR}/.*" \
    --exclude "${TEST_DIR}/.*" \
    --exclude "${ARDUINO_LIB_DIR}/tools/.*" \
    --print-summary \
    --html-details "${PROJECT_ROOT}/coverage/arduino/index.html" \
    --xml "${PROJECT_ROOT}/coverage/arduino/coverage.xml" \
    --object-directory "${BUILD_DIR}"

echo "[coverage_arduino] Reporte generado en coverage/arduino/index.html"
