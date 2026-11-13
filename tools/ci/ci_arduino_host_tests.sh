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
    BUILD_DIR="${ARDUINO_HOST_BUILD_DIR:-${LIB_DIR}/build-host-local}"
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
    --py-client "${ROOT_DIR}/mcubridge-client-examples/mcubridge_client/protocol.py"

echo "[host-cpp] Installing library dependencies..."
ARDUINO_LIBS="${ARDUINO_LIB_DIR:-$HOME/Arduino/libraries}"
mkdir -p "${ARDUINO_LIBS}"
"${LIB_DIR}/tools/install.sh" "${ARDUINO_LIBS}"

# Define explicit include paths for official libraries
ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
PIGWEED_PATH="$ARDUINO_LIBS/Pigweed"
WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl"
if [ ! -d "$WOLFSSL_PATH" ]; then WOLFSSL_PATH="$ARDUINO_LIBS/wolfSSL"; fi
if [ -d "$WOLFSSL_PATH/src/wolfcrypt/src" ]; then
    WOLFCRYPT_SRC="$WOLFSSL_PATH/src/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH/src"
else
    WOLFCRYPT_SRC="$WOLFSSL_PATH/wolfcrypt/src"
    WOLFSSL_INC="$WOLFSSL_PATH"
fi
WOLF_SOURCES=(
    "$WOLFCRYPT_SRC/sha256.c"
    "$WOLFCRYPT_SRC/hmac.c"
    "$WOLFCRYPT_SRC/kdf.c"
    "$WOLFCRYPT_SRC/chacha.c"
    "$WOLFCRYPT_SRC/poly1305.c"
    "$WOLFCRYPT_SRC/chacha20_poly1305.c"
    "$WOLFCRYPT_SRC/hash.c"
    "$WOLFCRYPT_SRC/misc.c"
)
mapfile -t PIGWEED_RPC_SOURCES < <(
    find "$PIGWEED_PATH/src" -maxdepth 1 -type f \
        \( -name 'pw_rpc_*.cc' -o -name 'pw_protobuf_*.cc' \
           -o -name 'pw_stream_interval_reader.cc' \
           -o -name 'pw_varint_stream.cc' \
           -o -name 'pw_containers_intrusive_item.cc' \) -print | sort
)

if [ "$INSTALL_ONLY" = true ]; then
    echo "[host-cpp] Dependencies installed. Exiting as requested by --install-only."
    exit 0
fi

SOURCES=(
    "${SRC_DIR}/security/security.cpp"
    "${WOLF_SOURCES[@]}"
    "${SRC_DIR}/pb_encode.c"
    "${SRC_DIR}/pb_decode.c"
    "${SRC_DIR}/pb_common.c"
    "${SRC_DIR}/protocol/config_schema.pb.c"
    "${SRC_DIR}/protocol/mcu_wire.pb.c"
    "${SRC_DIR}/hal/hal.cpp"
    "${SRC_DIR}/fsm/bridge_fsm.cpp"
    "${SRC_DIR}/Bridge.cpp"
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
    "$PIGWEED_PATH/src/system_clock_arduino.cpp"
    "$PIGWEED_PATH/src/log_arduino.cpp"
    "${PIGWEED_RPC_SOURCES[@]}"
    "$PIGWEED_PATH/src/pw_checksum_crc32.cc"
    "$PIGWEED_PATH/src/pw_hdlc_decoder.cc"
    "$PIGWEED_PATH/src/pw_hdlc_encoder.cc"
    "$PIGWEED_PATH/src/pw_varint_varint.cc"
    "$PIGWEED_PATH/src/pw_status_status.cc"
    "$PIGWEED_PATH/src/pw_tokenizer_encode_args.cc"
    "$PIGWEED_PATH/src/pw_tokenizer_tokenize.cc"
    "$PIGWEED_PATH/src/pw_stream_memory_stream.cc"
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
    -I"$PIGWEED_PATH"
    -I"$PIGWEED_PATH/src"
    -I"$WOLFSSL_INC"
    -I"$WOLFSSL_PATH"
)

COMMON_DEFS=(
    -Wall
    -Wextra
    -Werror
    -Wno-unused-parameter
    -DARDUINO_STUB_CUSTOM_MILLIS=1
    -DWOLFSSL_USER_SETTINGS
    -DETL_NO_STL
    -DETL_FORCE_STD_INITIALIZER_LIST
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
        -DARDUINO=100 -DBRIDGE_HOST_TEST=1 -DETL_NO_STL
        "${COMMON_DEFS[@]}"
        "${INC_FLAGS[@]}"
    )
else
    BASE_FLAGS=(
        -O2 -g
        -DARDUINO=100 -DBRIDGE_HOST_TEST=1 -DBRIDGE_DEBUG_IO=1
        -DBRIDGE_ENABLE_CONSOLE=1 -DBRIDGE_ENABLE_DATASTORE=1
        -DBRIDGE_ENABLE_MAILBOX=1 -DBRIDGE_ENABLE_FILESYSTEM=1
        -DBRIDGE_ENABLE_PROCESS=1 -DBRIDGE_ENABLE_SPI=1
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
COMPILE_PIDS=()
for src in "${SOURCES[@]}"; do
    obj_name=$(basename "${src}").o
    if [[ "${src}" == "$WOLFSSL_PATH/"* ]]; then
        obj_name="${src#"$WOLFSSL_PATH/"}"
        obj_name="${obj_name//\//_}.o"
    fi
    obj="${OBJ_DIR}/${obj_name}"
    OBJECTS+=("${obj}")

    if [ "$COVERAGE_MODE" = true ] && [[ "${src}" == *pb_* || "${src}" == *.pb.c ]]; then
        g++ -std=c++17 -x c++ "${TP_FLAGS[@]}" -c "${src}" -o "${obj}" &
    elif [[ "${src}" == "$WOLFSSL_PATH/"* ]]; then
        gcc -std=c99 "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}" &
    elif [[ "${src}" == *.c ]]; then
        g++ -std=c++17 -x c++ "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}" &
    else
        g++ -std=c++17 "${BASE_FLAGS[@]}" -c "${src}" -o "${obj}" &
    fi
    COMPILE_PIDS+=("$!")
done

# Wait for every compiler process; plain "wait" only reports the last child
# and can otherwise allow linking against missing object files.
compile_failed=0
for pid in "${COMPILE_PIDS[@]}"; do
    if ! wait "${pid}"; then
        compile_failed=1
    fi
done
if [ "${compile_failed}" -ne 0 ]; then
    echo "[host-cpp] COMMON SOURCE COMPILATION FAILED; refusing to link incomplete objects." >&2
    exit 1
fi

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

    gcovr_status=0
    ${PYTHON_CMD} -m gcovr --root "${SRC_DIR}" "${BUILD_DIR}" --filter "${SRC_DIR}" \
        -e ".*\\.h$" -e ".*etl.*" -e ".*rpc_protocol\.h" -e ".*wolfssl.*" -e ".*wolfcrypt.*" \
        --exclude-unreachable-branches --exclude-throw-branches --merge-mode-functions=merge-use-line-max \
        --sort uncovered-percent \
        --fail-under-line "${ARDUINO_COVERAGE_MIN_LINE}" \
        --fail-under-branch "${ARDUINO_COVERAGE_MIN_BRANCH}" \
        --fail-under-function "${ARDUINO_COVERAGE_MIN_FUNCTION}" \
        --html-details "${OUTPUT_ROOT}/index.html" \
        --json-summary "${OUTPUT_ROOT}/summary.json" --json-summary-pretty \
        --json "${OUTPUT_ROOT}/coverage.json" \
        --txt "${OUTPUT_ROOT}/coverage.txt" \
        --print-summary > "${OUTPUT_ROOT}/summary.txt" || gcovr_status=$?

    # Per-file table so threshold failures can be diagnosed from the CI log alone.
    cat "${OUTPUT_ROOT}/coverage.txt" || true
    cat "${OUTPUT_ROOT}/summary.txt"
    if [ "${gcovr_status}" -ne 0 ]; then
        exit "${gcovr_status}"
    fi
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
