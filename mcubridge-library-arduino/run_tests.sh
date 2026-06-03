#!/bin/bash
set -e
# [MIL-SPEC/SIL-2] McuBridge Library Local Test Runner
# Mission: Provide a robust, fast, and consistent test execution environment
# mirroring the CI (GitHub Actions) but optimized for local development.

# Get the script and repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$SCRIPT_DIR"

# --- Nanopb Core C Files ---
# Since these are ignored by .gitignore, we download them dynamically if missing.
install_nanopb_core() {
    local target_dir="src"
    local version="nanopb-0.4.9.1"
    local base_url="https://raw.githubusercontent.com/nanopb/nanopb/${version}"
    local files=(
        "pb.h"
        "pb_common.h"
        "pb_common.c"
        "pb_decode.h"
        "pb_decode.c"
        "pb_encode.h"
        "pb_encode.c"
    )

    mkdir -p "$target_dir"
    for f in "${files[@]}"; do
        local dest="$target_dir/$f"
        if [ ! -f "$dest" ]; then
            echo "[INFO] Downloading Nanopb core file: $f..."
            if command -v curl >/dev/null 2>&1; then
                curl -fsSL "$base_url/$f" -o "$dest"
            elif command -v wget >/dev/null 2>&1; then
                wget -qO "$dest" "$base_url/$f"
            else
                echo "[ERROR] 'curl' or 'wget' is required to download Nanopb." >&2
                exit 1
            fi
        fi
    done
}

install_nanopb_core

# Generate protocol bindings if missing or if python is available
if [ ! -f "src/protocol/rpc_protocol.h" ] || [ ! -f "src/protocol/mcubridge.pb.c" ]; then
    echo "[host-cpp] Protocol files missing. Generating..."
    PYTHON_CMD=$(command -v python3 || command -v python)
    if [ -n "$PYTHON_CMD" ]; then
        $PYTHON_CMD ../tools/protocol/generate.py \
            --spec ../tools/protocol/spec.toml \
            --py ../mcubridge/mcubridge/protocol/protocol.py \
            --cpp src/protocol/rpc_protocol.h \
            --cpp-structs src/protocol/rpc_structs.h \
            --py-client ../mcubridge-client-examples/mcubridge_client/protocol.py
    else
        echo "[ERROR] Python 3 is required to generate missing protocol files." >&2
        exit 1
    fi
fi

# Detect Arduino libraries directory
ARDUINO_LIBS="$HOME/Arduino/libraries"
if [ ! -d "$ARDUINO_LIBS" ]; then ARDUINO_LIBS="$HOME/Documents/Arduino/libraries"; fi

# 1. Embedded Template Library (ETL)
ETL_PATH="$ARDUINO_LIBS/Embedded_Template_Library"
if [ ! -d "$ETL_PATH" ]; then ETL_PATH="$REPO_ROOT/.dummy_libs/Embedded_Template_Library"; fi

# 2. wolfSSL (official)
WOLFSSL_PATH="$ARDUINO_LIBS/wolfSSL"
if [ ! -d "$WOLFSSL_PATH" ]; then WOLFSSL_PATH="$ARDUINO_LIBS/wolfssl"; fi
if [ ! -d "$WOLFSSL_PATH" ]; then WOLFSSL_PATH="$REPO_ROOT/.dummy_libs/wolfSSL"; fi
# 3. PacketSerial
PSERIAL_PATH="$ARDUINO_LIBS/PacketSerial"
if [ ! -d "$PSERIAL_PATH" ]; then PSERIAL_PATH="$REPO_ROOT/.dummy_libs/PacketSerial"; fi

# 4. Unity
# 5. Build Artifacts
BUILD_DIR="build-host-local"
OBJ_DIR="${BUILD_DIR}/objs"
mkdir -p "${OBJ_DIR}"

COMMON_FLAGS="-O2 -g -Wall -DBRIDGE_HOST_TEST=1 -DUNITY_INCLUDE_DOUBLE -DBRIDGE_ENABLE_SPI=1 -DWOLFSSL_USER_SETTINGS -DETL_NO_STL -Isrc -Isrc/config -Isrc/protocol -Itests/Unity/src -I../tools/arduino_stub/include -I$ETL_PATH -I$ETL_PATH/include -I$ETL_PATH/arduino -I$WOLFSSL_PATH -I$PACKETSERIAL_PATH -I$PACKETSERIAL_PATH/src"

SOURCES=(
    "src/security/security.cpp"
    "src/hal/hal.cpp"
    "src/protocol/rle.cpp"
    "src/Bridge.cpp"
    "src/BridgeInstance.cpp"
    "src/fsm/bridge_fsm.cpp"
    "src/services/Console.cpp"
    "src/services/DataStore.cpp"
    "src/services/Mailbox.cpp"
    "src/services/FileSystem.cpp"
    "src/services/Process.cpp"
    "src/services/SPIService.cpp"
    "../tools/arduino_stub/BridgeFaultInjection.cpp"
    "../tools/arduino_stub/ArduinoStubs.cpp"
    "tests/test_host_filesystem_mock.cpp"
    "$WOLFSSL_PATH/wolfcrypt/src/sha256.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hmac.c"
    "$WOLFSSL_PATH/wolfcrypt/src/hash.c"
    "$WOLFSSL_PATH/wolfcrypt/src/kdf.c"
    "$WOLFSSL_PATH/wolfcrypt/src/error.c"
    "$WOLFSSL_PATH/wolfcrypt/src/logging.c"
    "$WOLFSSL_PATH/wolfcrypt/src/wc_port.c"
    "$WOLFSSL_PATH/wolfcrypt/src/memory.c"
    "$WOLFSSL_PATH/wolfcrypt/src/chacha.c"
    "$WOLFSSL_PATH/wolfcrypt/src/poly1305.c"
    "$WOLFSSL_PATH/wolfcrypt/src/chacha20_poly1305.c"
    "src/pb_encode.c"
    "src/pb_decode.c"
    "src/pb_common.c"
    "src/protocol/mcubridge.pb.c"
)

# Unity test framework
UNITY_OBJ="${OBJ_DIR}/unity.o"
if [ ! -f "$UNITY_OBJ" ]; then
    gcc -c -O2 -DUNITY_INCLUDE_DOUBLE "tests/Unity/src/unity.c" -o "$UNITY_OBJ"
fi

echo "[host-cpp] Compiling common sources..."
OBJECTS=()
for src in "${SOURCES[@]}"; do
    obj_name=$(basename "${src}")
    obj="${OBJ_DIR}/${obj_name}.o"
    OBJECTS+=("${obj}")
    
    if [[ ! -f "$obj" || "$src" -nt "$obj" ]]; then
        if [[ "${src}" == *.c ]]; then
            gcc $COMMON_FLAGS -c "${src}" -o "${obj}"
        else
            g++ -std=c++17 $COMMON_FLAGS -c "${src}" -o "${obj}"
        fi
    fi
done

TESTS="test_protocol test_bridge_core test_bridge_components test_fsm_mutual_auth test_integrated test_host_filesystem test_arduino_100_coverage test_coverage_full test_coverage_hardened"

for t in $TESTS; do
    echo "=== Building $t ==="
    g++ -std=c++17 $COMMON_FLAGS "tests/${t}.cpp" "${OBJECTS[@]}" "${UNITY_OBJ}" -o "${BUILD_DIR}/${t}"
    echo "=== Running $t ==="
    "./${BUILD_DIR}/${t}"
    echo "=== $t DONE ==="
done

echo "ALL_TESTS_PASSED"
