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

# [CI/CD] Managed Dependencies Directory
DEPS_DIR="$PWD/openwrt-library-arduino/deps"
mkdir -p "$DEPS_DIR"

ensure_lib_git() {
    local name="$1"
    local url="$2"
    local target="$DEPS_DIR/$name"
    if [ ! -d "$target" ]; then
        echo "[CI] Fetching $name from $url..."
        git clone --depth 1 "$url" "$target"
        # SIL-2: Ensure deterministic structure for Arduino-cli
        # If include exists but src doesn't, link include to src
        if [ -d "$target/include" ] && [ ! -d "$target/src" ]; then
            echo "[CI] Creating src symlink for $name..."
            ln -s include "$target/src"
        fi
    fi
}

echo "Installing libraries..."
# Standard libraries that follow Arduino layout
arduino-cli lib install "PacketSerial" "FastCRC" "Crypto" "TaskScheduler"

# ETL has a non-standard layout in its main repo, we fix it locally
ensure_lib_git "ETL" "https://github.com/ETLCPP/etl.git"

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
    
    # We pass the ETL dependency path explicitly as a library
    BUILD_FLAGS="--fqbn $FQBN --library $LIB_PATH --library $DEPS_DIR/ETL --warnings default"
    
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
