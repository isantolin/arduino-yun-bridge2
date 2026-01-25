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
    fi
}

echo "Installing libraries..."
arduino-cli lib install "PacketSerial" "FastCRC" "Crypto" "TaskScheduler"
ensure_lib_git "ETL" "https://github.com/ETLCPP/etl.git"

# Define library path (current repo's library folder)
LIB_PATH="$PWD/openwrt-library-arduino"

# Compile examples
FQBN="arduino:avr:uno"
EXAMPLES_DIR="$LIB_PATH/examples"
BUILD_OUTPUT_DIR="${1:-}"

# [SIL-2] Target-specific configuration for ETL
# Header include path
ETL_INC="$DEPS_DIR/ETL/include"

echo "Compiling examples for $FQBN..."

find "$EXAMPLES_DIR" -name "*.ino" | while read sketch; do
    sketch_dir=$(dirname "$sketch")
    sketch_name=$(basename "$sketch_dir")
    
    echo "--------------------------------------------------"
    echo "Building $sketch_name..."
    
    # We pass the build property with proper escaping for arduino-cli
    # This combines includes and ETL configuration macros into a single property.
    # Format: --build-property "compiler.cpp.extra_flags=-Ipath -DMACRO=1"
    
    if [ -n "$BUILD_OUTPUT_DIR" ]; then
        SKETCH_BUILD_DIR="$BUILD_OUTPUT_DIR/$sketch_name"
        mkdir -p "$SKETCH_BUILD_DIR"
        
        arduino-cli compile \
            --fqbn "$FQBN" \
            --library "$LIB_PATH" \
            --build-path "$SKETCH_BUILD_DIR" \
            --build-property "compiler.cpp.extra_flags=-I$ETL_INC -DETL_NO_STL -DETL_THROW_EXCEPTIONS=0" \
            --warnings default \
            "$sketch"
    else
        arduino-cli compile \
            --fqbn "$FQBN" \
            --library "$LIB_PATH" \
            --build-property "compiler.cpp.extra_flags=-I$ETL_INC -DETL_NO_STL -DETL_THROW_EXCEPTIONS=0" \
            --warnings default \
            "$sketch"
    fi
        
    if [ $? -ne 0 ]; then
        echo "✗ $sketch_name failed to compile!"
        exit 1
    fi
    
    echo "✓ $sketch_name compiled successfully"
done

echo "--------------------------------------------------"
echo "All examples compiled successfully!"