cmake_minimum_required(VERSION 3.20)
project(mcubridge_gateway_cpp LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(OpenSSL 3.0 REQUIRED COMPONENTS SSL Crypto)
find_package(Threads REQUIRED)

set(PIGWEED_ROOT
    "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/avr_pigweed_port/third_party/pigweed"
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
set(pw_log_BACKEND pw_log_null CACHE STRING "" FORCE)
set(pw_chrono.system_clock_BACKEND pw_chrono_stl.system_clock CACHE STRING "" FORCE)
set(pw_chrono.system_timer_BACKEND pw_chrono_stl.system_timer CACHE STRING "" FORCE)
set(pw_sync.mutex_BACKEND pw_sync_stl.mutex_backend CACHE STRING "" FORCE)
set(pw_sync.binary_semaphore_BACKEND pw_sync_stl.binary_semaphore_backend CACHE STRING "" FORCE)
set(pw_sync.thread_notification_BACKEND pw_sync.binary_semaphore_thread_notification_backend CACHE STRING "" FORCE)
set(pw_sync.timed_thread_notification_BACKEND pw_sync.binary_semaphore_timed_thread_notification_backend CACHE STRING "" FORCE)
set(pw_thread.id_BACKEND pw_thread_stl.id CACHE STRING "" FORCE)
set(pw_thread.thread_BACKEND pw_thread_stl.thread CACHE STRING "" FORCE)
set(pw_thread.thread_creation_BACKEND pw_thread_stl.thread_creation CACHE STRING "" FORCE)
set(pw_thread.sleep_BACKEND pw_thread_stl.sleep CACHE STRING "" FORCE)

include("${PIGWEED_ROOT}/pw_build/pigweed.cmake")
include("${PIGWEED_ROOT}/pw_protobuf_compiler/proto.cmake")
set(pw_protobuf_compiler_PYTHON_OUT_DIR "${CMAKE_BINARY_DIR}/generated" CACHE PATH "Shared Pigweed generated Python proto modules" FORCE)
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
add_subdirectory("${PIGWEED_ROOT}/pw_chrono" pw_chrono)
add_subdirectory("${PIGWEED_ROOT}/pw_thread" pw_thread)
add_subdirectory("${PIGWEED_ROOT}/pw_thread_stl" pw_thread_stl)
add_subdirectory("${PIGWEED_ROOT}/pw_chrono_stl" pw_chrono_stl)
add_subdirectory("${PIGWEED_ROOT}/pw_checksum" pw_checksum)
add_subdirectory("${PIGWEED_ROOT}/pw_protobuf" pw_protobuf)
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/pigweed_gateway.cmake")

add_executable(mcubridge-gateway-cpp src/gateway_main.cc)
target_link_libraries(mcubridge-gateway-cpp PRIVATE
  OpenSSL::SSL OpenSSL::Crypto Threads::Threads
  pw_hdlc pw_rpc.server pw_transfer
  pw_thread.thread pw_thread_stl.thread pw_sync.mutex pw_sync.binary_semaphore
  pw_sync.timed_thread_notification pw_chrono.system_clock pw_chrono.system_timer
)
target_compile_options(mcubridge-gateway-cpp PRIVATE -Wall -Wextra -Wpedantic)
