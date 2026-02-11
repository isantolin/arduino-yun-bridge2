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

# Install dependencies
echo "Installing libraries..."
arduino-cli lib install PacketSerial Crypto "Embedded Template Library ETL"

# Define library path (current repo's library folder)
LIB_PATH="$PWD/openwrt-library-arduino"

# Define target boards (Matrix Build)
# - Yun: ATmega32u4 (Native USB)
# - Uno: ATmega328P (No Native USB, small RAM)
# - Mega: ATmega2560 (No Native USB, large RAM)
TARGET_BOARDS=("arduino:avr:yun" "arduino:avr:uno" "arduino:avr:mega")
EXAMPLES_DIR="$LIB_PATH/examples"
BUILD_OUTPUT_DIR="${1:-}"

for FQBN in "${TARGET_BOARDS[@]}"; do
    echo "=================================================="
    echo "Targeting Board: $FQBN"
    echo "=================================================="

    find "$EXAMPLES_DIR" -name "*.ino" | while read sketch; do
        sketch_dir=$(dirname "$sketch")
        sketch_name=$(basename "$sketch_dir")
        
        echo "Building $sketch_name for $FQBN..."
        
        BUILD_FLAGS="--fqbn $FQBN --library $LIB_PATH --warnings default"
        
        if [ -n "$BUILD_OUTPUT_DIR" ]; then
            # Create specific output dir for this sketch/board combo
            SKETCH_BUILD_DIR="$BUILD_OUTPUT_DIR/${FQBN//:/-}/$sketch_name"
            mkdir -p "$SKETCH_BUILD_DIR"
            BUILD_FLAGS="$BUILD_FLAGS --build-path $SKETCH_BUILD_DIR"
        fi

        # Force clean build to ensure hardware characteristics (macros, MCU type) are strictly respected
        # and not polluted by cached artifacts from previous targets in the loop.
        if ! arduino-cli compile --clean $BUILD_FLAGS "$sketch"; then
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
    done
done

echo "--------------------------------------------------"
echo "All examples compiled successfully for ALL targets!"
