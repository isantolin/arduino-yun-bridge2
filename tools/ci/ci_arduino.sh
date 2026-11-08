#!/bin/bash
# NOTE: set -e is intentionally omitted; parallel background jobs + wait
# require manual exit-code handling. pipefail is still active.
set -o pipefail

# Ensure we are in the repo root
cd "$(dirname "$0")/../.."

echo "Initializing Arduino CI environment..."

# Check for arduino-cli (auto-install locally if missing)
if ! command -v arduino-cli &> /dev/null; then
    echo "[ci_arduino] arduino-cli not found. Installing into ~/.local/bin..."
    mkdir -p "${HOME}/.local/bin"
    curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="${HOME}/.local/bin" sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

# Get standard library path
USER_LIB_DIR="$HOME/Arduino/libraries"
if [ ! -d "$USER_LIB_DIR" ]; then
    # Try alternate path for some linux distros/actions
    USER_LIB_DIR="$HOME/Documents/Arduino/libraries"
fi

# Update core index and install cores only if not already installed by CI workflow
if [ ! -d "$HOME/.arduino15/packages/arduino/hardware/avr" ] || \
   [ ! -d "$HOME/.arduino15/packages/arduino/hardware/samd" ] || \
   [ ! -d "$HOME/.arduino15/packages/arduino/hardware/esp32" ]; then
    echo "Updating core index..."
    if ! arduino-cli core update-index; then
        echo "[WARN] arduino-cli core update-index failed; bootstrapping package_index.tar.bz2 directly..."
        mkdir -p "$HOME/.arduino15"
        curl -fsSL "https://downloads.arduino.cc/packages/package_index.tar.bz2" | tar -xjf - -C "$HOME/.arduino15"
    fi

    echo "Installing Arduino AVR, SAMD, and ESP32 cores..."
    arduino-cli core install arduino:avr arduino:samd arduino:esp32 || {
        echo "Failed to install required Arduino board cores" >&2
        exit 1
    }
fi

# Install official dependencies if missing
if [ ! -d "$USER_LIB_DIR/WiFiNINA" ]; then
    echo "Installing official libraries..."
    arduino-cli lib install WiFiNINA || { echo "Failed to install WiFiNINA"; exit 1; }
fi

# Install dependencies if not already generated/installed by CI workflow
if [ ! -f "./mcubridge-library-arduino/src/protocol/rpc_protocol.h" ]; then
    echo "Generating protocol bindings..."
    python3 ./tools/protocol/generate.py \
        --spec ./tools/protocol/cloud_gateway.proto \
        --py ./mcubridge/mcubridge/protocol/protocol.py \
        --cpp ./mcubridge-library-arduino/src/protocol/rpc_protocol.h \
        --py-client mcubridge-client-examples/mcubridge_client/protocol.py
fi

if [ ! -d "$USER_LIB_DIR/PigweedAVR" ] || [ ! -d "$USER_LIB_DIR/Embedded_Template_Library" ]; then
    echo "Installing libraries..."
    # We pass USER_LIB_DIR to install.sh to ensure it installs there
    ./mcubridge-library-arduino/tools/install.sh "$USER_LIB_DIR"
fi

# Define library path (current repo's library folder)
LIB_PATH="$PWD/mcubridge-library-arduino"

# Define memory-capable, library-supported target boards (SSOT: tools.boards).
if command -v python3 >/dev/null 2>&1 && python3 -c "import tools.boards" >/dev/null 2>&1; then
    TARGET_BOARDS=($(python3 -m tools.boards fqbns))
else
    TARGET_BOARDS=("arduino:avr:mega" "arduino:samd:mkrwifi1010" "arduino:esp32:nano_nora")
fi
EXAMPLES_DIR="$LIB_PATH/examples"
BUILD_OUTPUT_DIR="${1:-}"

# Convert to absolute path if not empty
if [ -n "$BUILD_OUTPUT_DIR" ]; then
    mkdir -p "$BUILD_OUTPUT_DIR"
    BUILD_OUTPUT_DIR=$(cd "$BUILD_OUTPUT_DIR" && pwd)
fi

# Only shift if an argument was provided
if [ "$#" -gt 0 ]; then
    shift
fi

# All remaining arguments are treated as extra build properties
EXTRA_PROPS=()
while [ "$#" -gt 0 ]; do
    EXTRA_PROPS+=("--build-property" "$1")
    shift
done

# Maximum parallel compilation jobs
MAX_JOBS=${MAX_JOBS:-4}

# Temp dir for build logs
_LOG_DIR=$(mktemp -d)
trap 'rm -rf "$_LOG_DIR"' EXIT

# Compile a single sketch for a given board.
# Runs as a subshell so it can be backgrounded.
compile_sketch() {
    local FQBN="$1"
    local sketch="$2"
    local sketch_dir sketch_name BOARD_NAME LOG_FILE

    sketch_dir=$(dirname "$sketch")
    sketch_name=$(basename "$sketch_dir")
    BOARD_NAME="${FQBN//:/-}"
    LOG_FILE="${_LOG_DIR}/${BOARD_NAME}__${sketch_name}.log"

    local COMMON_FLAGS="-fno-strict-aliasing -DPB_BUFFER_ONLY=1 -DPB_NO_ERRMSG=1"
    local LINK_FLAGS=()
    local AVR_PROPS=()
    local AVR_STDCPP_INC=""
    if [[ "$FQBN" == arduino:avr:* ]]; then
        COMMON_FLAGS+=" -flto -Wno-lto-type-mismatch -DSERIAL_RX_BUFFER_SIZE=256 -DSERIAL_TX_BUFFER_SIZE=256 -D__cpp_lib_endian=201907L -DETL_FORCE_STD_INITIALIZER_LIST"
        if command -v avr-gcc >/dev/null 2>&1; then
            local SYS_AVR_GCC_VER="$(avr-gcc -dumpversion | cut -d. -f1)"
            if [ "$SYS_AVR_GCC_VER" -ge 8 ]; then
                local SYS_AVR_GCC_PATH="$(dirname "$(dirname "$(command -v avr-gcc)")")"
                AVR_PROPS+=("--build-property" "runtime.tools.avr-gcc.path=${SYS_AVR_GCC_PATH}")
                if [ "$SYS_AVR_GCC_VER" -ge 16 ] && [ -d "$USER_LIB_DIR/PigweedAVR/gcc16/include" ]; then
                    AVR_STDCPP_INC="-I$USER_LIB_DIR/PigweedAVR/gcc16/include"
                elif [ "$SYS_AVR_GCC_VER" -ge 14 ] && [ -d "$USER_LIB_DIR/PigweedAVR/gcc15/include" ]; then
                    AVR_STDCPP_INC="-I$USER_LIB_DIR/PigweedAVR/gcc15/include"
                elif [ "$SYS_AVR_GCC_VER" -ge 13 ] && [ -d "$USER_LIB_DIR/PigweedAVR/gcc13/include" ]; then
                    AVR_STDCPP_INC="-I$USER_LIB_DIR/PigweedAVR/gcc13/include"
                elif [ "$SYS_AVR_GCC_VER" -ge 11 ] && [ -d "$USER_LIB_DIR/PigweedAVR/gcc12/include" ]; then
                    AVR_STDCPP_INC="-I$USER_LIB_DIR/PigweedAVR/gcc12/include"
                elif [ -d "$USER_LIB_DIR/PigweedAVR/gcc10/include" ]; then
                    AVR_STDCPP_INC="-I$USER_LIB_DIR/PigweedAVR/gcc10/include"
                fi
            fi
        fi
        LINK_FLAGS=(
            "--build-property" "compiler.c.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch"
            "--build-property" "compiler.cpp.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch"
            "--build-property" "compiler.elf.extra_flags=-flto -fno-strict-aliasing -Wno-lto-type-mismatch"
        )
    fi
    local BUILD_FLAGS=(
        "--fqbn" "$FQBN"
        "--library" "$LIB_PATH"
        "--libraries" "$USER_LIB_DIR"
        "--warnings" "default"
        "${AVR_PROPS[@]}"
        "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions $COMMON_FLAGS -DETL_NO_STL $AVR_STDCPP_INC -I$USER_LIB_DIR/Embedded_Template_Library/include"
        "--build-property" "compiler.c.extra_flags=-std=gnu11 $COMMON_FLAGS $AVR_STDCPP_INC -I$USER_LIB_DIR/Embedded_Template_Library/include"
    )
    BUILD_FLAGS+=("${LINK_FLAGS[@]}")

    BUILD_FLAGS+=("${EXTRA_PROPS[@]}")

    if [ -n "$BUILD_OUTPUT_DIR" ]; then
        local SKETCH_BUILD_DIR="$BUILD_OUTPUT_DIR/${BOARD_NAME}/$sketch_name"
        mkdir -p "$SKETCH_BUILD_DIR"
        BUILD_FLAGS+=("--build-path" "$SKETCH_BUILD_DIR")
    else
        # Use a unique build path per board×sketch to avoid cache races
        # when multiple arduino-cli instances run in parallel.
        local ISOLATED_BUILD_DIR="${_LOG_DIR}/build/${BOARD_NAME}/${sketch_name}"
        mkdir -p "$ISOLATED_BUILD_DIR"
        BUILD_FLAGS+=("--build-path" "$ISOLATED_BUILD_DIR")
    fi

    local BOARD_CACHE_DIR="${_LOG_DIR}/cache/${BOARD_NAME}"
    mkdir -p "$BOARD_CACHE_DIR"
    BUILD_FLAGS+=("--build-cache-path" "$BOARD_CACHE_DIR")

    if arduino-cli compile "${BUILD_FLAGS[@]}" "$sketch" > "$LOG_FILE" 2>&1; then
        echo "✓ $sketch_name ($FQBN)"
        if [ -n "${ARDUINO_METRICS_DIR:-}" ]; then
            mkdir -p "$ARDUINO_METRICS_DIR"
            cp "$LOG_FILE" "$ARDUINO_METRICS_DIR/${BOARD_NAME}__${sketch_name}.log"
        fi
        return 0
    else
        if [ -n "${ARDUINO_METRICS_DIR:-}" ]; then
            mkdir -p "$ARDUINO_METRICS_DIR"
            cp "$LOG_FILE" "$ARDUINO_METRICS_DIR/${BOARD_NAME}__${sketch_name}.log"
        fi
        echo "✗ $sketch_name failed for $FQBN!"
        cat "$LOG_FILE" >&2
        return 1
    fi
}

# Collect all sketches, then compile each board in its own parallel worker
# so sketches for the same board reuse BOARD_CACHE_DIR sequentially without core.a races.
echo "Compiling examples in parallel across ${#TARGET_BOARDS[@]} target boards..."
pids=()
CRITICAL_FAIL=0

SKETCHES=()
while IFS= read -r s; do
    [ -n "$s" ] && SKETCHES+=("$s")
done < <(find "$EXAMPLES_DIR" -name "*.ino" | sort)

compile_board() {
    local FQBN="$1"
    local board_fail=0
    for sketch in "${SKETCHES[@]}"; do
        compile_sketch "$FQBN" "$sketch" || board_fail=1
    done
    return $board_fail
}

for FQBN in "${TARGET_BOARDS[@]}"; do
    compile_board "$FQBN" &
    pids+=($!)
done

# Wait for all board workers
for pid in "${pids[@]}"; do
    wait "$pid" || CRITICAL_FAIL=1
done

if [[ $CRITICAL_FAIL -ne 0 ]]; then
    echo "Critical compilation failure detected. Aborting."
    exit 1
fi

echo "--------------------------------------------------"
echo "All examples compiled successfully for ALL targets!"
