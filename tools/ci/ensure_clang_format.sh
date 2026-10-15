#!/usr/bin/env bash
set -euo pipefail

# Check if clang-format is in PATH strictly (no fallbacks or sudo mutations)
if ! command -v clang-format &> /dev/null; then
    echo "[ERROR] 'clang-format' binary not found in PATH." >&2
    exit 1
fi

echo "[ensure_clang_format] clang-format is ready."

# Format C++ and Arduino files in the repository
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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
