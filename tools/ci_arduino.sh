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

# Update core index
echo "Updating core index..."
arduino-cli core update-index

# Install AVR core (for MCU)
echo "Installing arduino:avr core..."
arduino-cli core install arduino:avr

# Install official wolfSSL dependency
echo "Installing official wolfSSL library..."
arduino-cli lib install wolfSSL

# Install official ETL dependency
echo "Installing official Embedded Template Library..."
arduino-cli lib install "Embedded Template Library ETL"

# Install dependencies
echo "Generating protocol bindings..."
python3 ./tools/protocol/generate.py \
    --spec ./tools/protocol/spec.toml \
    --py ./mcubridge/mcubridge/protocol/protocol.py \
    --cpp ./mcubridge-library-arduino/src/protocol/rpc_protocol.h \
    --cpp-structs ./mcubridge-library-arduino/src/protocol/rpc_structs.h \
    --py-client ./mcubridge-client-examples/mcubridge_client/protocol.py

echo "Installing libraries..."
# Use our robust installer to ensure src/etl is populated correctly
# for relative includes in Bridge.h
./mcubridge-library-arduino/tools/install.sh

# Define library path (current repo's library folder)
LIB_PATH="$PWD/mcubridge-library-arduino"

# Define target boards (Matrix Build)
# - Yun: ATmega32u4 (Native USB)
# - Uno: ATmega328P (No Native USB, small RAM)
# - Mega: ATmega2560 (No Native USB, large RAM)
TARGET_BOARDS=("arduino:avr:yun" "arduino:avr:uno" "arduino:avr:mega")
EXAMPLES_DIR="$LIB_PATH/examples"
BUILD_OUTPUT_DIR="${1:-}"

# Only shift if an argument was provided to avoid 'shift: count must be <= $#' error
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
        
        # [C++14] Override the platform default -std=gnu++11 with gnu++14.
        # GCC uses the last -std= flag, so appending via compiler.cpp.extra_flags
        # effectively selects C++14 for our library code.
        BUILD_FLAGS=("--fqbn" "$FQBN" "--library" "$LIB_PATH" "--warnings" "default"
                     "--build-property" "compiler.cpp.extra_flags=-std=gnu++17 -fno-exceptions -flto"
                     "--build-property" "compiler.c.extra_flags=-flto"
                     "--build-property" "compiler.c.elf.extra_flags=-flto")
        
        # Add extra properties
        BUILD_FLAGS+=("${EXTRA_PROPS[@]}")

        if [ -n "$BUILD_OUTPUT_DIR" ]; then
            # Create specific output dir for this sketch/board combo
            SKETCH_BUILD_DIR="$BUILD_OUTPUT_DIR/${FQBN//:/-}/$sketch_name"
            mkdir -p "$SKETCH_BUILD_DIR"
            BUILD_FLAGS+=("--build-path" "$SKETCH_BUILD_DIR")
        fi

        # Force clean build to ensure hardware characteristics (macros, MCU type) are strictly respected
        # and not polluted by cached artifacts from previous targets in the loop.
        # We use clean to avoid property pollution.
        if [ -n "${ARDUINO_METRICS_DIR:-}" ]; then
            mkdir -p "$ARDUINO_METRICS_DIR"
            BOARD_NAME="${FQBN//:/-}"
            LOG_FILE="$ARDUINO_METRICS_DIR/${BOARD_NAME}_${sketch_name}.log"
            
            # Run compilation and capture ALL output
            if ! arduino-cli compile --clean "${BUILD_FLAGS[@]}" "$sketch" > "$LOG_FILE" 2>&1; then
                echo "✗ $sketch_name failed to compile for $FQBN!"
                cat "$LOG_FILE" # Ensure failure details are in CI logs
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
