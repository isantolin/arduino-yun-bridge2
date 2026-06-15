#!/usr/bin/env bash
set -euo pipefail

# Check if clang-format is in PATH
if ! command -v clang-format &> /dev/null; then
    echo "[ensure_clang_format] clang-format not found. Attempting to install..."
    # 1. Try pip (since we are likely in a python virtualenv or python environment)
    if command -v pip &> /dev/null; then
        echo "[ensure_clang_format] Installing clang-format via pip..."
        pip install clang-format || true
    fi
fi

# Re-check if clang-format is in PATH
if ! command -v clang-format &> /dev/null; then
    # 2. Try system package managers
    if command -v dnf &> /dev/null; then
        echo "[ensure_clang_format] Installing clang-format via dnf..."
        sudo dnf install -y clang-format || true
    elif command -v apt-get &> /dev/null; then
        echo "[ensure_clang_format] Installing clang-format via apt-get..."
        sudo apt-get update && sudo apt-get install -y clang-format || true
    else
        echo "[ERROR] Could not install clang-format. Please install it manually." >&2
        exit 1
    fi
fi

if ! command -v clang-format &> /dev/null; then
    echo "[ERROR] clang-format still not found in PATH after installation attempts." >&2
    exit 1
fi

echo "[ensure_clang_format] clang-format is ready."

# Format C++ and Arduino files in the repository
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "[ensure_clang_format] Formatting C++ files..."

# Find and format all .cpp, .h, and .ino files, excluding Unity and Nanopb library files
find "${ROOT_DIR}/mcubridge-library-arduino/src" \
     "${ROOT_DIR}/mcubridge-library-arduino/tests" \
     "${ROOT_DIR}/mcubridge-library-arduino/examples" \
     \( -name "*.cpp" -o -name "*.h" -o -name "*.ino" \) \
     -not -path "*/Unity/*" \
     -not -path "*/src/protocol/mcubridge.pb.*" \
     -not -path "*/src/pb_*" \
     -print0 | xargs -0 clang-format -i

echo "[ensure_clang_format] C++ formatting complete."
