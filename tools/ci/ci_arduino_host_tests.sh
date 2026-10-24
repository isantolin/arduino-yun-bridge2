#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB_DIR="${ROOT_DIR}/mcubridge-library-arduino"
SRC_DIR="${LIB_DIR}/src"
TEST_DIR="${LIB_DIR}/tests"
STUB_DIR="${ROOT_DIR}/tools/arduino_stub/include"

COVERAGE_MODE=false
INSTALL_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --coverage)
            COVERAGE_MODE=true
            ;;
        --install-only)
            INSTALL_ONLY=true
            ;;
    esac
done

# Use system Python directly
PYTHON_CMD=$(command -v python3 || command -v python)

if [ "$COVERAGE_MODE" = true ]; then
    ARDUINO_COVERAGE_RUN_ID="${ARDUINO_COVERAGE_RUN_ID:-$$}"
    BUILD_DIR="${ROOT_DIR}/coverage/build-arduino-${ARDUINO_COVERAGE_RUN_ID}"
    OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
    ARDUINO_COVERAGE_MIN_LINE="${ARDUINO_COVERAGE_MIN_LINE:-95}"
    ARDUINO_COVERAGE_MIN_BRANCH="${ARDUINO_COVERAGE_MIN_BRANCH:-95}"
    ARDUINO_COVERAGE_MIN_FUNCTION="${ARDUINO_COVERAGE_MIN_FUNCTION:-95}"
    rm -rf "${BUILD_DIR}"
else
    BUILD_DIR="${LIB_DIR}/build-host-local"
    OUTPUT_ROOT=""
fi

OBJ_DIR="${BUILD_DIR}/objs"
METADATA_DIR="${BUILD_DIR}/generated"
mkdir -p "${OBJ_DIR}" "${METADATA_DIR}"
if [ "$COVERAGE_MODE" = true ]; then
    mkdir -p "${OUTPUT_ROOT}"
fi

python3 "${ROOT_DIR}/tools/arduino_core_metadata.py" \
    --host-test \
    --write-header "${METADATA_DIR}/ArduinoCoreMetadata.h"
cp -f "${METADATA_DIR}/ArduinoCoreMetadata.h" "${STUB_DIR}/ArduinoCoreMetadata.h"

# [SIL-2] Ensure dependencies are present
echo "[host-cpp] Generating protocol bindings..."
${PYTHON_CMD} "${ROOT_DIR}/tools/protocol/generate.py" \
    --spec "${ROOT_DIR}/tools/protocol/cloud_gateway.proto" \
    --py "${ROOT_DIR}/mcubridge/mcubridge/protocol/protocol.py" \
    --cpp "${SRC_DIR}/protocol/rpc_protocol.h" \
    --cpp-structs "${SRC_DIR}/protocol/rpc_structs.h" \
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"

echo "[host-cpp] Installing library dependencies..."
ARDUINO_LIBS="${ARDUINO_LIB_DIR:-$HOME/Arduino/libraries}"
mkdir -p "${ARDUINO_LIBS}"
"${LIB_DIR}/tools/install.sh" "${ARDUINO_LIBS}"

# Define explicit include paths for official libraries
ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfSSL"
if [ ! -d "$WOLFSSL_PATH" ]; then WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl"; fi
if [ -d "$WOLFSSL_PATH/src/wolfcrypt/src" ]; then
    WOLFCRYPT_SRC="$WOLFSSL_PATH/src/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH/src"
else
    WOLFCRYPT_SRC="$WOLFSSL_PATH/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH"
fi
PACKETSERIAL_PATH="$ARDUINO_LIBS/PacketSerial"

if [ "$INSTALL_ONLY" = true ]; then
    echo "[host-cpp] Dependencies installed. Exiting as requested by --install-only."
    exit 0
fi

SOURCES=(
    "${SRC_DIR}/security/security.cpp"
    "$WOLFCRYPT_SRC/sha256.c"
    "$WOLFCRYPT_SRC/hmac.c"
    "$WOLFCRYPT_SRC/hash.c"
    "$WOLFCRYPT_SRC/kdf.c"
    "$WOLFCRYPT_SRC/error.c"
    "$WOLFCRYPT_SRC/logging.c"
    "$WOLFCRYPT_SRC/wc_port.c"
    "$WOLFCRYPT_SRC/memory.c"
    "$WOLFCRYPT_SRC/chacha.c"
    "$WOLFCRYPT_SRC/poly1305.c"
    "$WOLFCRYPT_SRC/chacha20_poly1305.c"
    "${SRC_DIR}/pb_encode.c"
    "${SRC_DIR}/pb_decode.c"
    "${SRC_DIR}/pb_common.c"
    "${SRC_DIR}/protocol/config_schema.pb.c"
    "${SRC_DIR}/protocol/mcu_wire.pb.c"
    "${SRC_DIR}/hal/hal.cpp"
    "${SRC_DIR}/fsm/bridge_fsm.cpp"
    "${SRC_DIR}/Bridge.cpp"
    "${SRC_DIR}/Instantiations.cpp"
    "${TEST_DIR}/bridge_test_global.cpp"
    "${SRC_DIR}/services/Console.cpp"
    "${SRC_DIR}/services/DataStore.cpp"
    "${SRC_DIR}/services/Mailbox.cpp"
    "${SRC_DIR}/services/FileSystem.cpp"
    "${SRC_DIR}/services/Process.cpp"
    "${SRC_DIR}/services/SPIService.cpp"
    "${ROOT_DIR}/tools/arduino_stub/BridgeFaultInjection.cpp"
    "${TEST_DIR}/test_host_filesystem_mock.cpp"
    "${ROOT_DIR}/tools/arduino_stub/ArduinoStubs.cpp"
)

INC_FLAGS=(
    -I"${SRC_DIR}"
    -I"${SRC_DIR}/config"
    -I"${SRC_DIR}/protocol"
    -I"${TEST_DIR}/Unity/src"
    -I"${METADATA_DIR}"
    -I"${STUB_DIR}"
    -I"${TEST_DIR}"
    -I"$ETL_PATH"
    -I"$ETL_PATH/include"
    -I"$ETL_PATH/arduino"
    -I"$WOLFSSL_PATH"
    -I"$WOLFSSL_INC"
    -I"$PACKETSERIAL_PATH"
    -I"$PACKETSERIAL_PATH/src"
)

COMMON_DEFS=(
    -Wall
    -Wextra
    -Werror
    -Wno-unused-parameter
    -DBRIDGE_FAULT_INJECTION=1
    -DARDUINO_STUB_CUSTOM_MILLIS=1
    -DWOLFSSL_USER_SETTINGS
    -DETL_NO_STL
    -DUNITY_INCLUDE_DOUBLE
)

if [ "$COVERAGE_MODE" = true ]; then
    BASE_FLAGS=(
        -O0 -g -fprofile-arcs -ftest-coverage -fPIC
        -DARDUINO=100 -DBRIDGE_HOST_TEST=1 -DBRIDGE_DEBUG_IO=1
        -DBRIDGE_ENABLE_CONSOLE=1 -DBRIDGE_ENABLE_DATASTORE=1
        -DBRIDGE_ENABLE_MAILBOX=1 -DBRIDGE_ENABLE_FILESYSTEM=1
        -DBRIDGE_ENABLE_PROCESS=1 -DBRIDGE_ENABLE_SPI=1
        "${COMMON_DEFS[@]}"
        "${INC_FLAGS[@]}"
    )
    TP_FLAGS=(
        -O2 -g -fPIC
        -DARDUINO=100 -DBRIDGE_HOST_TEST=1 -DWOLFSSL_USER_SETTINGS -DETL_NO_STL
        "${COMMON_DEFS[@]}"
        "${INC_FLAGS[@]}"
    )
else
    BASE_FLAGS=(
        -O2 -g
        "${COMMON_DEFS[@]}"
        "${INC_FLAGS[@]}"
    )
fi

# Unity test framework
UNITY_DIR="${TEST_DIR}/Unity/src"
UNITY_OBJ="${OBJ_DIR}/unity.o"
if [ -f "${UNITY_DIR}/unity.c" ]; then
    g++ -std=c++17 -x c++ -c "${BASE_FLAGS[@]}" "${UNITY_DIR}/unity.c" -o "${UNITY_OBJ}"
else
    echo "[WARN] Unity not found at ${UNITY_DIR}; test assertions will fail."
    UNITY_OBJ=""
fi

# Compile common sources to objects in parallel
echo "[host-cpp] Compiling common sources in parallel..."
OBJECTS=()
for src in "${SOURCES[@]}"; do
    obj_name=$(basename "${src}").o
    obj="${OBJ_DIR}/${obj_name}"
    OBJECTS+=("${obj}")

    if [ "$COVERAGE_MODE" = true ] && [[ "${src}" == *wolfcrypt* || "${src}" == *pb_* || "${src}" == *.pb.c ]]; then
        g++ -std=c++17 -x c++ "${TP_FLAGS[@]}" -c "${src}" -o "${obj}" &
    elif [[ "${src}" == *.c ]]; then
        g++ -std=c++17 -x c++ "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}" &
    else
        g++ -std=c++17 "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}" &
    fi
done
wait

# Test suites
TEST_FILES=(
    "${TEST_DIR}/test_integrated.cpp"
    "${TEST_DIR}/test_bridge_core.cpp"
    "${TEST_DIR}/test_bridge_components.cpp"
    "${TEST_DIR}/test_host_filesystem.cpp"
    "${TEST_DIR}/test_protocol.cpp"
    "${TEST_DIR}/test_fsm_mutual_auth.cpp"
    "${TEST_DIR}/test_rpc_structs.cpp"
    "${TEST_DIR}/test_arduino_harden.cpp"
    "${TEST_DIR}/test_arduino_crypto_harden.cpp"
    "${TEST_DIR}/test_arduino_stress.cpp"
    "${TEST_DIR}/test_bridge_edge_paths.cpp"
    "${TEST_DIR}/test_hal_weak_defaults.cpp"
)

if [ "$COVERAGE_MODE" = true ]; then
    echo "[host-cpp] Compiling and running test suites sequentially for coverage..."
    pushd "${BUILD_DIR}" > /dev/null
    for test_file in "${TEST_FILES[@]}"; do
        test_name=$(basename "${test_file}" .cpp)
        LOCAL_OBJECTS=()
        for obj in "${OBJECTS[@]}"; do
            if [ "${test_name}" = "test_hal_weak_defaults" ] && [[ "$(basename "${obj}")" == "test_host_filesystem_mock.cpp.o" ]]; then
                continue
            fi
            LOCAL_OBJECTS+=("${obj}")
        done
        g++ -std=c++17 "${BASE_FLAGS[@]}" "${test_file}" "${LOCAL_OBJECTS[@]}" "${UNITY_OBJ}" -o "${BUILD_DIR}/${test_name}"
        "${BUILD_DIR}/${test_name}"
    done
    popd > /dev/null

    echo "[host-cpp] Running gcovr for coverage analysis..."
    if ! ${PYTHON_CMD} -m gcovr --version >/dev/null 2>&1; then
        echo "[host-cpp] Installing gcovr..."
        ${PYTHON_CMD} -m pip install --break-system-packages gcovr
    fi

    ${PYTHON_CMD} -m gcovr --root "${SRC_DIR}" "${BUILD_DIR}" --filter "${SRC_DIR}" \
        -e ".*\\.h$" -e ".*etl.*" -e ".*wolfssl.*" -e ".*wolfcrypt.*" -e ".*rpc_protocol\.h" -e ".*rpc_structs\.h" \
        --exclude-unreachable-branches --exclude-throw-branches --merge-mode-functions=merge-use-line-max \
        --sort uncovered-percent \
        --fail-under-line "${ARDUINO_COVERAGE_MIN_LINE}" \
        --fail-under-branch "${ARDUINO_COVERAGE_MIN_BRANCH}" \
        --fail-under-function "${ARDUINO_COVERAGE_MIN_FUNCTION}" \
        --html-details "${OUTPUT_ROOT}/index.html" \
        --json-summary "${OUTPUT_ROOT}/summary.json" --json-summary-pretty \
        --json "${OUTPUT_ROOT}/coverage.json" \
        --print-summary > "${OUTPUT_ROOT}/summary.txt"

    cat "${OUTPUT_ROOT}/summary.txt"
    echo "[host-cpp] ALL COVERAGE GATES PASSED (Line >= ${ARDUINO_COVERAGE_MIN_LINE}%, Branch >= ${ARDUINO_COVERAGE_MIN_BRANCH}%, Function >= ${ARDUINO_COVERAGE_MIN_FUNCTION}%)"
else
    # Compile and run test suites in parallel
    echo "[host-cpp] Compiling and running test suites in parallel..."
    pids=()
    test_names=()
    for test_file in "${TEST_FILES[@]}"; do
        (
            test_name=$(basename "${test_file}" .cpp)
            if [ "${test_name}" = "test_hal_weak_defaults" ]; then
                LOCAL_OBJECTS=()
                for obj in "${OBJECTS[@]}"; do
                    if [[ "$(basename "${obj}")" != "test_host_filesystem_mock.cpp.o" ]]; then
                        LOCAL_OBJECTS+=("${obj}")
                    fi
                done
                g++ -std=c++17 "${BASE_FLAGS[@]}" "${test_file}" "${LOCAL_OBJECTS[@]}" "${UNITY_OBJ}" -o "${BUILD_DIR}/${test_name}" 2>&1
            else
                g++ -std=c++17 "${BASE_FLAGS[@]}" "${test_file}" "${OBJECTS[@]}" "${UNITY_OBJ}" -o "${BUILD_DIR}/${test_name}" 2>&1
            fi
            if [ $? -eq 0 ]; then
                :
            else
                status=$?
                echo "[host-cpp] COMPILE FAILED: ${test_name} (exit ${status})" >&2
                exit "${status}"
            fi
            if "${BUILD_DIR}/${test_name}"; then
                :
            else
                status=$?
                echo "[host-cpp] TEST FAILED: ${test_name} (exit ${status})" >&2
                exit "${status}"
            fi
        ) &
        pids+=($!)
        test_names+=("$(basename "${test_file}" .cpp)")

        # Throttle to MAX_JOBS
        if [[ ${#pids[@]} -ge 5 ]]; then
            if ! wait ${pids[0]}; then
                echo "[host-cpp] FAILED: ${test_names[0]}" >&2
                exit 1
            fi
            pids=("${pids[@]:1}")
            test_names=("${test_names[@]:1}")
        fi
    done

    # Wait for remaining
    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}"; then
            echo "[host-cpp] FAILED: ${test_names[$i]}" >&2
            exit 1
        fi
    done

    echo "[host-cpp] ALL HOST TESTS PASSED"
fi
