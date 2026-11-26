cmake_minimum_required(VERSION 3.20)
project(mcubridge_gateway_cpp LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
include(FetchContent)

# Use fresh extraction timestamps on CMake versions that support CMP0135.
if(POLICY CMP0135)
  cmake_policy(SET CMP0135 NEW)
endif()

# Full wolfSSL TLS 1.3 for Linux; MCU targets keep their own WOLFCRYPT_ONLY config.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "Build wolfSSL as a static library" FORCE)
set(WOLFSSL_EXAMPLES no CACHE STRING "" FORCE)
set(WOLFSSL_CRYPT_TESTS no CACHE STRING "" FORCE)
set(WOLFSSL_OPENSSLEXTRA yes CACHE STRING "" FORCE)
set(WOLFSSL_TLS13 yes CACHE STRING "" FORCE)
set(WOLFSSL_RSA yes CACHE STRING "" FORCE)
set(WOLFSSL_ECC yes CACHE STRING "" FORCE)
set(WOLFSSL_ASN yes CACHE STRING "" FORCE)
set(WOLFSSL_FILESYSTEM yes CACHE STRING "" FORCE)
FetchContent_Declare(
  wolfssl
  URL https://downloads.sourceforge.net/project/wolfssl/v5.9.4-stable/wolfSSL%20Release%205.9.4%20%28September%2025%2C%202026%29%20source%20code.zip
  URL_HASH SHA256=c9ac97ef8bd1888d66460a2bf7bb941d4511d4cc0b964e9e2b4bdce9934696a1
)
FetchContent_MakeAvailable(wolfssl)
find_package(Threads REQUIRED)

get_filename_component(DEFAULT_PIGWEED_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/pigweed" ABSOLUTE)
set(PIGWEED_ROOT
    "${DEFAULT_PIGWEED_ROOT}"
    CACHE PATH "Pinned upstream Pigweed source tree")
if(NOT EXISTS "${PIGWEED_ROOT}/pw_rpc/CMakeLists.txt")
  message(FATAL_ERROR "Pigweed source is missing; initialize the recursive submodules or set PIGWEED_ROOT.")
endif()
set(ENV{PW_ROOT} "${PIGWEED_ROOT}")
set(pw_unit_test_ENABLE_PW_ADD_TEST OFF CACHE BOOL "" FORCE)
# Preserve the upstream header-only unit-test support target for test-only library declarations.
# Test executables remain disabled.
add_library(pw_unit_test INTERFACE)
target_include_directories(pw_unit_test INTERFACE "${PIGWEED_ROOT}/pw_unit_test/public")
set(pw_assert.assert_BACKEND pw_assert.print_and_abort_assert_backend CACHE STRING "" FORCE)
set(pw_assert.check_BACKEND pw_assert.print_and_abort_check_backend CACHE STRING "" FORCE)
set(pw_sys_io_BACKEND pw_sys_io_stdio CACHE STRING "" FORCE)
set(pw_log_BACKEND pw_log_basic CACHE STRING "" FORCE)
set(pw_chrono.system_clock_BACKEND pw_chrono_stl.system_clock CACHE STRING "" FORCE)
set(pw_chrono.system_timer_BACKEND pw_chrono_stl.system_timer CACHE STRING "" FORCE)
set(pw_sync.mutex_BACKEND pw_sync_stl.mutex_backend CACHE STRING "" FORCE)
set(pw_sync.binary_semaphore_BACKEND pw_sync_stl.binary_semaphore_backend CACHE STRING "" FORCE)
set(pw_sync.thread_notification_BACKEND pw_sync.binary_semaphore_thread_notification_backend CACHE STRING "" FORCE)
set(pw_sync.timed_thread_notification_BACKEND pw_sync.binary_semaphore_timed_thread_notification_backend CACHE STRING "" FORCE)
set(pw_sync.interrupt_spin_lock_BACKEND pw_sync_stl.interrupt_spin_lock CACHE STRING "" FORCE)
set(pw_thread.id_BACKEND pw_thread_stl.id CACHE STRING "" FORCE)
set(pw_thread.thread_BACKEND pw_thread_stl.thread CACHE STRING "" FORCE)
set(pw_thread.thread_creation_BACKEND pw_thread_stl.thread_creation CACHE STRING "" FORCE)
set(pw_thread.sleep_BACKEND pw_thread_stl.sleep CACHE STRING "" FORCE)

include("${PIGWEED_ROOT}/pw_build/pigweed.cmake")
include("${PIGWEED_ROOT}/pw_protobuf_compiler/proto.cmake")
set(pw_protobuf_compiler_PYTHON_OUT_DIR "${CMAKE_BINARY_DIR}/generated" CACHE PATH "Shared Pigweed generated Python proto modules" FORCE)

set(MCUBRIDGE_PROTOCOL_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../tools/protocol")
pw_proto_library(gateway_control_proto
  STRIP_PREFIX "${MCUBRIDGE_PROTOCOL_DIR}"
  SOURCES "${MCUBRIDGE_PROTOCOL_DIR}/gateway/gateway_control.proto"
  INPUTS "${MCUBRIDGE_PROTOCOL_DIR}/gateway/gateway_control.pwpb_options")
# Keep both source protos at the same import root so mcu_wire.proto can
# resolve its sibling import "config_schema.proto" in every generated language.
pw_proto_library(mcu_wire_proto
  STRIP_PREFIX "${MCUBRIDGE_PROTOCOL_DIR}"
  SOURCES
    "${MCUBRIDGE_PROTOCOL_DIR}/config_schema.proto"
    "${MCUBRIDGE_PROTOCOL_DIR}/mcu_wire.proto")
# pw_rpc encodes every outgoing packet in a buffer of
# PW_RPC_ENCODING_BUFFER_SIZE_BYTES (default 512). pw_transfer data chunks are
# up to kTransferChunkBytes (1024) plus chunk and packet headers; with the
# default, any read larger than one small chunk failed in Write() and the
# server aborted the transfer with DATA_LOSS. Match kRpcEncodeBytes.
add_compile_definitions(PW_RPC_ENCODING_BUFFER_SIZE_BYTES=2048)

add_subdirectory("${PIGWEED_ROOT}/pw_build" pw_build)
add_subdirectory("${PIGWEED_ROOT}/third_party/fuchsia" pw_third_party_fuchsia)
add_subdirectory("${PIGWEED_ROOT}/pw_numeric" pw_numeric)
add_subdirectory("${PIGWEED_ROOT}/pw_function" pw_function)
add_subdirectory("${PIGWEED_ROOT}/pw_memory" pw_memory)
add_subdirectory("${PIGWEED_ROOT}/pw_polyfill" pw_polyfill)
add_subdirectory("${PIGWEED_ROOT}/pw_preprocessor" pw_preprocessor)

add_subdirectory("${PIGWEED_ROOT}/pw_assert" pw_assert)
add_subdirectory("${PIGWEED_ROOT}/pw_result" pw_result)
add_subdirectory("${PIGWEED_ROOT}/pw_span" pw_span)
add_subdirectory("${PIGWEED_ROOT}/pw_bytes" pw_bytes)
add_subdirectory("${PIGWEED_ROOT}/pw_varint" pw_varint)
add_subdirectory("${PIGWEED_ROOT}/pw_string" pw_string)
add_subdirectory("${PIGWEED_ROOT}/pw_base64" pw_base64)
add_subdirectory("${PIGWEED_ROOT}/pw_chrono" pw_chrono)
add_subdirectory("${PIGWEED_ROOT}/pw_thread" pw_thread)
add_subdirectory("${PIGWEED_ROOT}/pw_thread_stl" pw_thread_stl)
add_subdirectory("${PIGWEED_ROOT}/pw_chrono_stl" pw_chrono_stl)
add_subdirectory("${PIGWEED_ROOT}/pw_checksum" pw_checksum)
add_subdirectory("${PIGWEED_ROOT}/pw_protobuf" pw_protobuf)
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/pigweed_gateway.cmake")

add_executable(mcubridge-gateway-cpp src/gateway_main.cc)
target_link_libraries(mcubridge-gateway-cpp PRIVATE
  gateway_control_proto.pwpb_rpc pw_rpc.pwpb.server_api pw_rpc.pwpb.client_api pw_protobuf
  mcu_wire_proto.raw_rpc pw_rpc.raw.client_api pw_rpc.client
  wolfssl Threads::Threads
  pw_assert pw_log pw_log_basic pw_hdlc pw_hdlc.rpc_channel_output
  pw_metric pw_metric.metric_service_pwpb pw_result
  pw_rpc.server pw_transfer
  pw_thread.thread pw_thread_stl.thread pw_sync.mutex pw_sync.lock_annotations
  pw_sync.scoped_locker pw_sync.binary_semaphore
  pw_sync.timed_thread_notification pw_chrono.system_clock pw_chrono.system_timer
)
target_compile_options(mcubridge-gateway-cpp PRIVATE
  -Wall -Wextra -Wpedantic
  -include features.h
  -include wolfssl/options.h
)
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  target_compile_options(mcubridge-gateway-cpp PRIVATE
    -Wthread-safety
    -Wthread-safety-beta
  )
endif()

option(ENABLE_COVERAGE "Enable code coverage instrumentation" OFF)
if(ENABLE_COVERAGE)
  target_compile_options(mcubridge-gateway-cpp PRIVATE --coverage -O0 -g)
  target_link_options(mcubridge-gateway-cpp PRIVATE --coverage)
endif()

include(GNUInstallDirs)
install(TARGETS mcubridge-gateway-cpp
  RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}"
)
install(FILES
  deploy/mcubridge-gateway.service
  deploy/mcubridge-gateway.env.example
  DESTINATION "${CMAKE_INSTALL_DATADIR}/mcubridge-gateway"
)
