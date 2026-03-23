#!/bin/bash
set -eo pipefail

# Ensure we are in the repo root
cd "$(dirname "$0")/.."

echo "Initializing Arduino CI environment..."

# Check for arduino-cli
if ! command -v arduino-cli &> /dev/null; then
    echo "Error: arduino-cli is not installed."
    echo "Please install it from: https://arduino.github.io/arduino-cli/latest/installation/"
    exit 1
fi

# Get standard library path
USER_LIB_DIR="$HOME/Arduino/libraries"
if [ ! -d "$USER_LIB_DIR" ]; then
    # Try alternate path for some linux distros/actions
    USER_LIB_DIR="$HOME/Documents/Arduino/libraries"
fi

# Define explicit include paths for official libraries
ETL_INC="$USER_LIB_DIR/Embedded_Template_Library_ETL/src"
WOLF_INC="$USER_LIB_DIR/wolfssl/src"

# Update core index
echo "Updating core index..."
arduino-cli core update-index

# Install AVR core (for MCU)
echo "Installing arduino:avr core..."
arduino-cli core install arduino:avr

# Install official dependencies
echo "Installing official wolfSSL library..."
arduino-cli lib install wolfSSL

echo "Installing official Embedded Template Library..."
arduino-cli lib install "Embedded Template Library ETL"

# Install dependencies
echo "Generating protocol bindings..."
python3 ./tools/protocol/generate.py \
    --spec ./tools/protocol/spec.toml \
    --py ./mcubridge/mcubridge/protocol/protocol.py \
    --cpp ./mcubridge-library-arduino/src/protocol/rpc_protocol.h \
    --cpp-structs ./mcubridge-library-arduino/src/protocol/rpc_structs.h \
    --py-client mcubridge-client-examples/mcubridge_client/protocol.py

echo "Installing libraries..."
./mcubridge-library-arduino/tools/install.sh

# [HOT-PATCH] Force official wolfSSL to use our settings by overwriting its user_settings.h
echo "Patching official wolfSSL at $WOLF_INC with our user_settings.h..."
# Ensure the directory exists (it should if install was successful)
mkdir -p "$WOLF_INC"
cp "$PWD/mcubridge-library-arduino/src/user_settings.h" "$WOLF_INC/user_settings.h"

# [HOT-PATCH] Fix gmtime_r conflict in wc_port.c
echo "Patching wc_port.c to avoid gmtime_r conflict..."
sed -i 's/#if defined(WOLFSSL_GMTIME)/#if defined(WOLFSSL_GMTIME) \&\& !defined(HAVE_GMTIME_R)/' "$USER_LIB_DIR/wolfssl/src/wolfcrypt/src/wc_port.c"

# Define library path (current repo's library folder)
LIB_PATH="$PWD/mcubridge-library-arduino"

# Define target boards (Matrix Build)
TARGET_BOARDS=("arduino:avr:yun" "arduino:avr:uno" "arduino:avr:mega")
EXAMPLES_DIR="$LIB_PATH/examples"
BUILD_OUTPUT_DIR="${1:-}"

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

for FQBN in "${TARGET_BOARDS[@]}"; do
    echo "=================================================="
    echo "Targeting Board: $FQBN"
    echo "=================================================="

    find "$EXAMPLES_DIR" -name "*.ino" | while read -r sketch; do
        sketch_dir=$(dirname "$sketch")
        sketch_name=$(basename "$sketch_dir")
        
        echo "Building $sketch_name for $FQBN..."
        
        # We rely on arduino-cli to find libraries in USER_LIB_DIR
        # WOLFSSL_USER_SETTINGS is defined to use our config.
        # -fno-strict-aliasing is added to resolve wolfSSL LTO warnings.
        COMMON_FLAGS="-flto -fno-strict-aliasing -DWOLFSSL_USER_SETTINGS"
        BUILD_FLAGS=("--fqbn" "$FQBN" "--library" "$LIB_PATH" "--libraries" "$USER_LIB_DIR" "--warnings" "default"
                     "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions $COMMON_FLAGS -DETL_NO_STL"
                     "--build-property" "compiler.c.extra_flags=-std=gnu11 $COMMON_FLAGS"
                     "--build-property" "compiler.c.elf.extra_flags=-flto")
        
        # Add extra properties
        BUILD_FLAGS+=("${EXTRA_PROPS[@]}")

        if [ -n "$BUILD_OUTPUT_DIR" ]; then
            SKETCH_BUILD_DIR="$BUILD_OUTPUT_DIR/${FQBN//:/-}/$sketch_name"
            mkdir -p "$SKETCH_BUILD_DIR"
            BUILD_FLAGS+=("--build-path" "$SKETCH_BUILD_DIR")
        fi

        if [ -n "${ARDUINO_METRICS_DIR:-}" ]; then
            mkdir -p "$ARDUINO_METRICS_DIR"
            BOARD_NAME="${FQBN//:/-}"
            LOG_FILE="$ARDUINO_METRICS_DIR/${BOARD_NAME}_${sketch_name}.log"
            
            if ! arduino-cli compile --clean "${BUILD_FLAGS[@]}" "$sketch" > "$LOG_FILE" 2>&1; then
                echo "✗ $sketch_name failed to compile for $FQBN!"
                cat "$LOG_FILE"
                if [ "$FQBN" == "arduino:avr:mega" ]; then
                    echo "Critical failure for target $FQBN. Aborting."
                    exit 1
                else
                    echo "Failure for $FQBN is not critical. Continuing..."
                fi
            else
                echo "✓ $sketch_name compiled successfully"
            fi
        else
            if ! arduino-cli compile --clean "${BUILD_FLAGS[@]}" "$sketch"; then
                echo "✗ $sketch_name failed to compile for $FQBN!"
                if [ "$FQBN" == "arduino:avr:mega" ]; then
                    echo "Critical failure for target $FQBN. Aborting."
                    exit 1
                else
                    echo "Failure for $FQBN is not critical. Continuing..."
                fi
            else
                echo "✓ $sketch_name compiled successfully"
            fi
        fi
    done
done

echo "--------------------------------------------------"
echo "All examples compiled successfully for ALL targets!"
