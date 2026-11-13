#!/usr/bin/env bash
# Install the official Espressif Arduino core used for ESP32-S3 targets.
#
# Upstream Pigweed needs GCC >= 10; the arduino:esp32 2.0.x core ships GCC 8,
# so ESP32 builds use the official Espressif core (3.x, modern xtensa-esp-elf
# toolchain). The version is pinned for reproducible CI.
set -euo pipefail

ESP32_INDEX_URL="https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json"
ESP32_CORE_VERSION="${ESP32_CORE_VERSION:-3.3.12}"

if arduino-cli core list 2>/dev/null | grep -Eq "^esp32:esp32[[:space:]]+${ESP32_CORE_VERSION//./\\.}[[:space:]]"; then
    echo "[esp32-core] esp32:esp32@${ESP32_CORE_VERSION} already installed"
    exit 0
fi

for attempt in 1 2 3 4 5; do
    if arduino-cli core update-index --additional-urls "$ESP32_INDEX_URL" &&
        arduino-cli core install "esp32:esp32@${ESP32_CORE_VERSION}" --additional-urls "$ESP32_INDEX_URL"; then
        echo "[esp32-core] installed esp32:esp32@${ESP32_CORE_VERSION}"
        exit 0
    fi
    delay=$((attempt * 15))
    echo "[esp32-core] install failed (attempt ${attempt}/5); retrying in ${delay}s" >&2
    sleep "$delay"
done
echo "ERROR: could not install esp32:esp32@${ESP32_CORE_VERSION}" >&2
exit 1
