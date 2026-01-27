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
arduino-cli lib install PacketSerial CRC32 Crypto "Embedded Template Library ETL"

# Define library path (current repo's library folder)
LIB_PATH="$PWD/openwrt-library-arduino"

# Compile examples
FQBN="arduino:avr:uno"
EXAMPLES_DIR="$LIB_PATH/examples"
BUILD_OUTPUT_DIR="${1:-}"

echo "Compiling examples for $FQBN..."

find "$EXAMPLES_DIR" -name "*.ino" | while read sketch; do
    sketch_dir=$(dirname "$sketch")
    sketch_name=$(basename "$sketch_dir")
    
    echo "--------------------------------------------------"
    echo "Building $sketch_name..."
    
    BUILD_FLAGS="--fqbn $FQBN --library $LIB_PATH --warnings default"
    
    if [ -n "$BUILD_OUTPUT_DIR" ]; then
        # Create specific output dir for this sketch to avoid overwrites
        SKETCH_BUILD_DIR="$BUILD_OUTPUT_DIR/$sketch_name"
        mkdir -p "$SKETCH_BUILD_DIR"
        BUILD_FLAGS="$BUILD_FLAGS --build-path $SKETCH_BUILD_DIR"
    fi

    arduino-cli compile $BUILD_FLAGS "$sketch"
        
    if [ $? -ne 0 ]; then
        echo "✗ $sketch_name failed to compile!"
        exit 1
    fi
    
    echo "✓ $sketch_name compiled successfully"
done

echo "--------------------------------------------------"
echo "All examples compiled successfully!"
