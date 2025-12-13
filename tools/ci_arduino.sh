#!/bin/bash
set -e

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

# Install AVR core (for Yun)
echo "Installing arduino:avr core..."
arduino-cli core install arduino:avr

# Install dependencies
echo "Installing libraries..."
arduino-cli lib install PacketSerial CRC32 Crypto

# Define library path (current repo's library folder)
LIB_PATH="$PWD/openwrt-library-arduino"

# Compile examples
FQBN="arduino:avr:yun"
EXAMPLES_DIR="$LIB_PATH/examples"

echo "Compiling examples for $FQBN..."

find "$EXAMPLES_DIR" -name "*.ino" | while read sketch; do
    sketch_dir=$(dirname "$sketch")
    sketch_name=$(basename "$sketch_dir")
    
    echo "--------------------------------------------------"
    echo "Building $sketch_name..."
    
    arduino-cli compile --fqbn "$FQBN" \
        --library "$LIB_PATH" \
        --warnings all \
        "$sketch"
        
    if [ $? -eq 0 ]; then
        echo "✓ $sketch_name compiled successfully"
    else
        echo "✗ $sketch_name failed to compile"
        exit 1
    fi
done

echo "--------------------------------------------------"
echo "All examples compiled successfully!"
