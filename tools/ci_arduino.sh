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
# Standard libraries via CLI
arduino-cli lib install "PacketSerial" "FastCRC" "Crypto" "TaskScheduler"

# Fetch ETL from source
ensure_lib_git "ETL" "https://github.com/ETLCPP/etl.git"

# Define library path (current repo's library folder)
LIB_PATH="$PWD/openwrt-library-arduino"

# Compile examples
FQBN="arduino:avr:uno"
EXAMPLES_DIR="$LIB_PATH/examples"
BUILD_OUTPUT_DIR="${1:-}"

# [SIL-2] Target-specific configuration for ETL
# We force ETL to not use the System STL because AVR (uno) doesn't provide it.
# This prevents the 'type_traits: No such file or directory' error.
EXTRA_INCLUDES="-I$DEPS_DIR/ETL/include"
COMPILER_FLAGS="-DETL_NO_STL -DETL_THROW_EXCEPTIONS=0"

echo "Compiling examples for $FQBN..."

find "$EXAMPLES_DIR" -name "*.ino" | while read sketch; do
    sketch_dir=$(dirname "$sketch")
    sketch_name=$(basename "$sketch_dir")
    
    echo "--------------------------------------------------"
    echo "Building $sketch_name..."
    
    # We pass the ETL dependency path explicitly as a compiler flag to avoid
    # arduino-cli library discovery issues.
    # Note: we wrap the properties in quotes to prevent shell/CLI misinterpretation.
    BUILD_FLAGS="--fqbn $FQBN --library $LIB_PATH --build-property compiler.cpp.extra_flags=$EXTRA_INCLUDES $COMPILER_FLAGS --warnings default"
    
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
