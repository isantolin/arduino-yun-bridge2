#!/usr/bin/env bash
# Shared toolchain selection for 32-bit Arduino targets and host builds (sourced, not executed).
#
# Upstream (mainstream) Pigweed, used on SAMD and ESP32 since the per-architecture
# Pigweed split, requires a modern GCC: C++17 library traits such as
# std::has_unique_object_representations_v and a working PW_CONSTINIT. The
# arduino:samd core still ships arm-none-eabi-gcc 7-2017q4, so SAMD builds use a
# modern system toolchain through a build-property override, the same technique
# these scripts already use for avr-gcc.
#
# pw_chrono backend selection (verified against toolchain libstdc++.a):
#   - SAMD21 (arm-none-eabi newlib): std::chrono::steady_clock::now() calls
#     gettimeofday(), which links against libnosys's stub _gettimeofday() that
#     always sets ENOSYS and returns -1 without populating struct timeval.
#     SAMD21 therefore uses tools/pw_chrono_arduino (Arduino millis()-backed,
#     64-bit rollover-safe, interrupt-masked).
#   - ESP32-S3 (xtensa-esp32s3-elf + ESP-IDF): std::chrono::steady_clock::now()
#     calls clock_gettime(CLOCK_MONOTONIC) -> esp_time_impl_get_time(), which is
#     ESP-IDF's 64-bit FreeRTOS/SMP-safe monotonic hardware timer. ESP32-S3 and
#     Linux host builds therefore use upstream pw_chrono_stl.

MCUBRIDGE_MIN_ARM_GCC_MAJOR=10

TOOLCHAIN_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLCHAIN_REPO_ROOT="$(cd "${TOOLCHAIN_SCRIPT_DIR}/../.." && pwd)"
PW_ROOT="${TOOLCHAIN_REPO_ROOT}/third_party/pigweed"
PW_CHRONO_ARDUINO_DIR="${TOOLCHAIN_REPO_ROOT}/tools/pw_chrono_arduino"

PW_COOPERATIVE_DEFINES=(
    -DPW_TRANSFER_COOPERATIVE=1
    -DPW_TRANSFER_COOPERATIVE_QUEUE_CAPACITY=2
    -DPW_TRANSFER_COOPERATIVE_CHUNK_BUFFER_SIZE=128
)
PW_COOPERATIVE_DEFS="${PW_COOPERATIVE_DEFINES[*]}"

PW_CHRONO_ARDUINO_INC_FLAGS=(
    -I"${PW_CHRONO_ARDUINO_DIR}/public_overrides"
    -I"${PW_ROOT}/pw_chrono/public"
)
PW_CHRONO_ARDUINO_SRCS=(
    "${PW_CHRONO_ARDUINO_DIR}/system_clock.cc"
)

PW_CHRONO_STL_INC_FLAGS=(
    -I"${PW_ROOT}/pw_chrono_stl/clock_public_overrides"
    -I"${PW_ROOT}/pw_chrono_stl/public"
    -I"${PW_ROOT}/pw_chrono/public"
)

PW_SYNC_STL_INC_FLAGS=(
    -I"${PW_ROOT}/pw_sync_stl/public_overrides"
    -I"${PW_ROOT}/pw_sync_stl/public"
)

# Common upstream Pigweed include paths (excluding pw_transfer and pw_chrono
# backend overrides, which are prepended by stage_cooperative_pw_transfer).
PW_COMMON_INC_FLAGS=(
    -I"${PW_ROOT}/pw_status/public"
    -I"${PW_ROOT}/pw_result/public"
    -I"${PW_ROOT}/pw_span/public"
    -I"${PW_ROOT}/pw_bytes/public"
    -I"${PW_ROOT}/pw_containers/public"
    -I"${PW_ROOT}/pw_stream/public"
    -I"${PW_ROOT}/pw_checksum/public"
    -I"${PW_ROOT}/pw_hdlc/public"
    -I"${PW_ROOT}/pw_rpc/public"
    -I"${PW_ROOT}/pw_rpc/nanopb/public"
    -I"${PW_ROOT}/pw_rpc/raw/public"
    -I"${PW_ROOT}/pw_varint/public"
    -I"${PW_ROOT}/pw_tokenizer/public"
    -I"${PW_ROOT}/pw_function/public"
    -I"${PW_ROOT}/pw_string/public"
    -I"${PW_ROOT}/pw_log/public"
    -I"${PW_ROOT}/pw_assert/print_and_abort_assert_public_overrides"
    -I"${PW_ROOT}/pw_assert/print_and_abort_check_public_overrides"
    -I"${PW_ROOT}/pw_assert/public"
    -I"${PW_ROOT}/pw_preprocessor/public"
    -I"${PW_ROOT}/pw_polyfill/public"
    -I"${PW_ROOT}/pw_enum/public"
    -I"${PW_ROOT}/pw_numeric/public"
    -I"${PW_ROOT}/pw_memory/public"
    -I"${PW_ROOT}/pw_protobuf/public"
    -I"${PW_ROOT}/pw_sync/public"
    -I"${PW_ROOT}/pw_sync/public_overrides"
    -I"${PW_ROOT}/pw_thread/public"
    -I"${PW_ROOT}/pw_toolchain/public"
    -I"${PW_ROOT}/third_party/fuchsia/repo/sdk/lib/stdcompat/include"
    -I"${PW_ROOT}/third_party/fuchsia/repo/sdk/lib/fit/include"
    -I"${TOOLCHAIN_REPO_ROOT}/build/pigweed_generated/include"
)

PW_INC_FLAGS=(
    "${PW_CHRONO_STL_INC_FLAGS[@]}"
    "${PW_COMMON_INC_FLAGS[@]}"
)
PW_INC="${PW_INC_FLAGS[*]}"

PW_TRANSFER_STAGED_INC_DIR=""
PW_TRANSFER_STAGED_SRC_DIR=""
PW_TRANSFER_STAGED_SRCS=()
PW_SAMD_INC_FLAGS=()
PW_SAMD_INC=""
PW_ESP32_INC_FLAGS=()
PW_ESP32_INC=""
PW_HOST_INC_FLAGS=()
PW_HOST_INC=""

PW_PIGWEED_SRCS=(
    "$PW_ROOT/pw_rpc/call.cc"
    "$PW_ROOT/pw_rpc/client.cc"
    "$PW_ROOT/pw_rpc/client_call.cc"
    "$PW_ROOT/pw_rpc/channel.cc"
    "$PW_ROOT/pw_rpc/channel_list.cc"
    "$PW_ROOT/pw_rpc/endpoint.cc"
    "$PW_ROOT/pw_rpc/packet.cc"
    "$PW_ROOT/pw_rpc/packet_meta.cc"
    "$PW_ROOT/pw_rpc/server.cc"
    "$PW_ROOT/pw_rpc/server_call.cc"
    "$PW_ROOT/pw_rpc/service.cc"
    "$PW_ROOT/pw_rpc/nanopb/common.cc"
    "$PW_ROOT/pw_rpc/nanopb/method.cc"
    "$PW_ROOT/pw_rpc/nanopb/server_reader_writer.cc"
    "$PW_ROOT/pw_rpc/raw/method.cc"
    "$PW_ROOT/pw_containers/intrusive_item.cc"
    "$PW_ROOT/pw_varint/stream.cc"
    "$PW_ROOT/pw_stream/interval_reader.cc"
    "$PW_ROOT/pw_protobuf/decoder.cc"
    "$PW_ROOT/pw_protobuf/encoder.cc"
    "$PW_ROOT/pw_protobuf/find.cc"
    "$PW_ROOT/pw_protobuf/message.cc"
    "$PW_ROOT/pw_protobuf/stream_decoder.cc"
    "$PW_ROOT/pw_checksum/crc32.cc"
    "$PW_ROOT/pw_hdlc/decoder.cc"
    "$PW_ROOT/pw_hdlc/encoder.cc"
    "$PW_ROOT/pw_varint/varint.cc"
    "$PW_ROOT/pw_status/status.cc"
    "$PW_ROOT/pw_tokenizer/encode_args.cc"
    "$PW_ROOT/pw_tokenizer/tokenize.cc"
    "$PW_ROOT/pw_log/rate_limited.cc"
    "$PW_ROOT/pw_stream/memory_stream.cc"
)

# Stages ONLY the cooperative pw_transfer headers and implementation sources
# exported by mcubridge-library-arduino/tools/install.sh into clean isolated
# directories so 32-bit/host builds never inherit AVR-only headers from
# PigweedAVR/src (avr-libstdcpp, pw_assert_avr, pw_chrono_avr).
stage_cooperative_pw_transfer() {
    local stage_root="$1"
    local arduino_libs="${2:-${ARDUINO_LIB_DIR:-${HOME}/Arduino/libraries}}"
    local export_src="${arduino_libs}/PigweedAVR/src"

    if [ ! -d "${export_src}/pw_transfer" ]; then
        echo "ERROR: exported cooperative pw_transfer headers missing at ${export_src}/pw_transfer (run mcubridge-library-arduino/tools/install.sh first)." >&2
        return 1
    fi
    local req_hdr
    for req_hdr in \
        pw_transfer/client.h \
        pw_transfer/handler.h \
        pw_transfer/transfer.h \
        pw_transfer/transfer_thread.h \
        pw_transfer/transfer.pwpb.h \
        pw_transfer/transfer.raw_rpc.pb.h \
        pw_log_avr/log_avr.h \
        pw_log_backend/log_backend.h; do
        if [ ! -f "${export_src}/${req_hdr}" ]; then
            echo "ERROR: exported cooperative pw_transfer/pw_log header missing: ${export_src}/${req_hdr}" >&2
            return 1
        fi
    done

    PW_TRANSFER_STAGED_INC_DIR="${stage_root}/pw_transfer_include"
    PW_TRANSFER_STAGED_SRC_DIR="${stage_root}/pw_transfer_sources"
    rm -rf "${PW_TRANSFER_STAGED_INC_DIR}" "${PW_TRANSFER_STAGED_SRC_DIR}"
    mkdir -p "${PW_TRANSFER_STAGED_INC_DIR}" "${PW_TRANSFER_STAGED_SRC_DIR}"
    cp -a "${export_src}/pw_transfer" "${PW_TRANSFER_STAGED_INC_DIR}/"
    cp -a "${export_src}/pw_log_avr" "${PW_TRANSFER_STAGED_INC_DIR}/"
    cp -a "${export_src}/pw_log_backend" "${PW_TRANSFER_STAGED_INC_DIR}/"
    find "${export_src}" -maxdepth 1 -type f \
        \( -name 'pw_transfer_*.cc' -o -name 'pw_transfer_*.cpp' -o -name 'log_avr.cpp' \) \
        -exec cp {} "${PW_TRANSFER_STAGED_SRC_DIR}/" \;
    mapfile -t PW_TRANSFER_STAGED_SRCS < <(
        find "${PW_TRANSFER_STAGED_SRC_DIR}" -maxdepth 1 -type f \
            \( -name 'pw_transfer_*.cc' -o -name 'pw_transfer_*.cpp' -o -name 'log_avr.cpp' \) -print | sort
    )
    if [ "${#PW_TRANSFER_STAGED_SRCS[@]}" -eq 0 ]; then
        echo "ERROR: no exported cooperative pw_transfer sources found in ${export_src}" >&2
        return 1
    fi

    PW_SAMD_INC_FLAGS=(
        -I"${PW_TRANSFER_STAGED_INC_DIR}"
        "${PW_CHRONO_ARDUINO_INC_FLAGS[@]}"
        "${PW_COMMON_INC_FLAGS[@]}"
    )
    PW_SAMD_INC="${PW_SAMD_INC_FLAGS[*]}"

    PW_ESP32_INC_FLAGS=(
        -I"${PW_TRANSFER_STAGED_INC_DIR}"
        "${PW_CHRONO_STL_INC_FLAGS[@]}"
        "${PW_COMMON_INC_FLAGS[@]}"
    )
    PW_ESP32_INC="${PW_ESP32_INC_FLAGS[*]}"

    PW_HOST_INC_FLAGS=(
        -I"${PW_TRANSFER_STAGED_INC_DIR}"
        "${PW_CHRONO_STL_INC_FLAGS[@]}"
        "${PW_SYNC_STL_INC_FLAGS[@]}"
        "${PW_COMMON_INC_FLAGS[@]}"
    )
    PW_HOST_INC="${PW_HOST_INC_FLAGS[*]}"

    PW_INC_FLAGS=("${PW_HOST_INC_FLAGS[@]}")
    PW_INC="${PW_HOST_INC}"
}

# Populates the global array SAMD_TOOLCHAIN_PROPS with the arduino-cli build
# properties that point the SAMD core at a modern arm-none-eabi toolchain.
# Returns non-zero (with an actionable message) when no suitable toolchain exists.
samd_toolchain_props() {
    SAMD_TOOLCHAIN_PROPS=()
    local gcc_bin
    gcc_bin="$(command -v arm-none-eabi-gcc || true)"
    if [ -z "$gcc_bin" ]; then
        echo "ERROR: arm-none-eabi-gcc >= ${MCUBRIDGE_MIN_ARM_GCC_MAJOR} is required for SAMD builds" \
            "(Debian/Ubuntu: apt-get install gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib; Fedora: dnf install arm-none-eabi-gcc-cs-c++ arm-none-eabi-newlib)." >&2
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

build_pigweed_archive_samd() {
    local output_archive="$1"
    local extra_inc="${2:-}"
    local arduino_libs="${3:-${ARDUINO_LIB_DIR:-${HOME}/Arduino/libraries}}"
    samd_toolchain_props || return 1
    local arm_gcc
    arm_gcc="$(command -v arm-none-eabi-gcc)"
    local arm_bin_dir
    arm_bin_dir="$(dirname "$arm_gcc")"
    local arm_gxx="${arm_bin_dir}/arm-none-eabi-g++"
    local arm_ar="${arm_bin_dir}/arm-none-eabi-ar"
    if [ ! -f "$arm_gxx" ]; then
        echo "ERROR: arm-none-eabi-g++ not found in ${arm_bin_dir}" >&2
        return 1
    fi
    if [ ! -f "$arm_ar" ]; then
        echo "ERROR: arm-none-eabi-ar not found in ${arm_bin_dir}" >&2
        return 1
    fi

    rm -f "$output_archive"
    local build_tmp
    build_tmp="$(mktemp -d /tmp/pw_samd_build_XXXXXX)"
    local staged_locally=0
    if [ -z "${PW_TRANSFER_STAGED_INC_DIR}" ] || [ ! -d "${PW_TRANSFER_STAGED_INC_DIR}/pw_transfer" ]; then
        stage_cooperative_pw_transfer "${build_tmp}/transfer" "${arduino_libs}" || {
            rm -rf "$build_tmp"
            return 1
        }
        staged_locally=1
    fi

    local objs=()
    local inc_args=("${PW_SAMD_INC_FLAGS[@]}")
    if [ -n "$extra_inc" ]; then
        inc_args+=("-I${extra_inc}")
    fi
    local all_srcs=(
        "${PW_PIGWEED_SRCS[@]}"
        "${PW_TRANSFER_STAGED_SRCS[@]}"
        "${PW_CHRONO_ARDUINO_SRCS[@]}"
    )

    local src obj
    for src in "${all_srcs[@]}"; do
        obj="${build_tmp}/${src//\//_}.o"
        "$arm_gxx" -mcpu=cortex-m0plus -mthumb -std=gnu++17 -fno-exceptions -fno-rtti \
            -ffunction-sections -fdata-sections \
            -DETL_NO_STL -DPB_BUFFER_ONLY=1 -DPB_NO_ERRMSG=1 -DPW_RPC_USE_GLOBAL_MUTEX=0 \
            "${PW_COOPERATIVE_DEFINES[@]}" -Os \
            "${inc_args[@]}" -c "$src" -o "$obj" || {
            rm -rf "$build_tmp"
            return 1
        }
        objs+=("$obj")
    done
    "$arm_ar" rcs "$output_archive" "${objs[@]}"
    if [ "$staged_locally" -eq 1 ]; then
        PW_TRANSFER_STAGED_INC_DIR=""
        PW_TRANSFER_STAGED_SRC_DIR=""
        PW_TRANSFER_STAGED_SRCS=()
    fi
    rm -rf "$build_tmp"
    echo "[toolchain] Built Pigweed + cooperative pw_transfer + millis pw_chrono archive for SAMD: $output_archive" >&2
}

find_esp32_toolchain() {
    XTENSA_GCC="$(find "${HOME}/.arduino15/packages/esp32/tools/esp-x32" "${HOME}/.arduino15/packages/esp32" -type f -name xtensa-esp32s3-elf-gcc 2>/dev/null | head -n 1 || true)"
    if [ -z "$XTENSA_GCC" ]; then
        echo "ERROR: xtensa-esp32s3-elf-gcc not found; run tools/ci/install_espressif_core.sh" >&2
        return 1
    fi
    local xtensa_bin_dir
    xtensa_bin_dir="$(dirname "$XTENSA_GCC")"
    XTENSA_GXX="${xtensa_bin_dir}/xtensa-esp32s3-elf-g++"
    if [ ! -f "$XTENSA_GXX" ]; then
        XTENSA_GXX="${xtensa_bin_dir}/xtensa-esp-elf-g++"
    fi
    XTENSA_AR="${xtensa_bin_dir}/xtensa-esp32s3-elf-ar"
    if [ ! -f "$XTENSA_AR" ]; then
        XTENSA_AR="${xtensa_bin_dir}/xtensa-esp-elf-ar"
    fi
}

build_pigweed_archive_esp32() {
    local output_archive="$1"
    local extra_inc="${2:-}"
    local arduino_libs="${3:-${ARDUINO_LIB_DIR:-${HOME}/Arduino/libraries}}"
    find_esp32_toolchain || return 1

    rm -f "$output_archive"
    local build_tmp
    build_tmp="$(mktemp -d /tmp/pw_esp32_build_XXXXXX)"
    local staged_locally=0
    if [ -z "${PW_TRANSFER_STAGED_INC_DIR}" ] || [ ! -d "${PW_TRANSFER_STAGED_INC_DIR}/pw_transfer" ]; then
        stage_cooperative_pw_transfer "${build_tmp}/transfer" "${arduino_libs}" || {
            rm -rf "$build_tmp"
            return 1
        }
        staged_locally=1
    fi

    local objs=()
    local inc_args=("${PW_ESP32_INC_FLAGS[@]}")
    if [ -n "$extra_inc" ]; then
        inc_args+=("-I${extra_inc}")
    fi
    local all_srcs=(
        "${PW_PIGWEED_SRCS[@]}"
        "${PW_TRANSFER_STAGED_SRCS[@]}"
    )

    local src obj
    for src in "${all_srcs[@]}"; do
        obj="${build_tmp}/${src//\//_}.o"
        "$XTENSA_GXX" -mdynconfig=xtensa_esp32s3.so -mlongcalls -std=gnu++20 -fno-exceptions -fno-rtti \
            -ffunction-sections -fdata-sections \
            -DETL_NO_STL -DPB_BUFFER_ONLY=1 -DPB_NO_ERRMSG=1 -DPW_RPC_USE_GLOBAL_MUTEX=0 \
            "${PW_COOPERATIVE_DEFINES[@]}" -Os \
            "${inc_args[@]}" -c "$src" -o "$obj" || {
            rm -rf "$build_tmp"
            return 1
        }
        objs+=("$obj")
    done
    "$XTENSA_AR" rcs "$output_archive" "${objs[@]}"
    if [ "$staged_locally" -eq 1 ]; then
        PW_TRANSFER_STAGED_INC_DIR=""
        PW_TRANSFER_STAGED_SRC_DIR=""
        PW_TRANSFER_STAGED_SRCS=()
    fi
    rm -rf "$build_tmp"
    echo "[toolchain] Built Pigweed + cooperative pw_transfer archive for ESP32: $output_archive" >&2
}
