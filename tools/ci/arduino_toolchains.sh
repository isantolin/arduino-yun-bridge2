#!/usr/bin/env bash
# Shared toolchain selection for 32-bit Arduino targets (sourced, not executed).
#
# Upstream (mainstream) Pigweed, used on SAMD and ESP32 since the per-architecture
# Pigweed split, requires a modern GCC: C++17 library traits such as
# std::has_unique_object_representations_v and a working PW_CONSTINIT. The
# arduino:samd core still ships arm-none-eabi-gcc 7-2017q4, so SAMD builds use a
# modern system toolchain through a build-property override, the same technique
# these scripts already use for avr-gcc.

MCUBRIDGE_MIN_ARM_GCC_MAJOR=10

# Populates the global array SAMD_TOOLCHAIN_PROPS with the arduino-cli build
# properties that point the SAMD core at a modern arm-none-eabi toolchain.
# Returns non-zero (with an actionable message) when no suitable toolchain exists.
samd_toolchain_props() {
    SAMD_TOOLCHAIN_PROPS=()
    local gcc_bin
    gcc_bin="$(command -v arm-none-eabi-gcc || true)"
    if [ -z "$gcc_bin" ]; then
        echo "ERROR: arm-none-eabi-gcc >= ${MCUBRIDGE_MIN_ARM_GCC_MAJOR} is required for SAMD builds" \
            "(Debian/Ubuntu: apt-get install gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib)." >&2
        return 1
    fi
    local major
    major="$("$gcc_bin" -dumpversion | cut -d. -f1)"
    if [ "$major" -lt "$MCUBRIDGE_MIN_ARM_GCC_MAJOR" ]; then
        echo "ERROR: arm-none-eabi-gcc ${major} is too old; upstream Pigweed needs >= ${MCUBRIDGE_MIN_ARM_GCC_MAJOR}." >&2
        return 1
    fi
    echo "[toolchain] SAMD: using $("$gcc_bin" --version | head -n 1) from $(dirname "$gcc_bin")" >&2
    SAMD_TOOLCHAIN_PROPS=("--build-property" "compiler.path=$(dirname "$gcc_bin")/")
}
