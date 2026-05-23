#include <unity.h>

#include "hal/hal.h"
#include "protocol/rpc_protocol.h"

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

void setUp() {}
void tearDown() {}

namespace bridge::hal {
__attribute__((weak)) bool g_host_has_sd = false;
__attribute__((weak)) bool g_host_fs_enabled = false;
}

void test_hal_weak_defaults_without_mock() {
  bridge::hal::g_host_fs_enabled = false;
  bridge::hal::g_host_has_sd = false;
  TEST_ASSERT_FALSE(bridge::hal::hasSD());

  etl::array<uint8_t, 4> data = {1, 2, 3, 4};
  auto wr = bridge::hal::writeFile(etl::string_view("weak.bin"),
                                   etl::span<const uint8_t>(data));
  TEST_ASSERT_FALSE(static_cast<bool>(wr));
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::hal::HalError::NOT_IMPLEMENTED),
      static_cast<int>(wr.error()));

  etl::array<uint8_t, 4> out = {};
  auto rd = bridge::hal::readFileChunk(etl::string_view("weak.bin"), 0,
                                       etl::span<uint8_t>(out));
  TEST_ASSERT_FALSE(static_cast<bool>(rd));
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::hal::HalError::NOT_IMPLEMENTED),
      static_cast<int>(rd.error()));

  auto rm = bridge::hal::removeFile(etl::string_view("weak.bin"));
  TEST_ASSERT_FALSE(static_cast<bool>(rm));
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(bridge::hal::HalError::NOT_IMPLEMENTED),
      static_cast<int>(rm.error()));

  const uint32_t caps = bridge::hal::getCapabilities();
  TEST_ASSERT_EQUAL_UINT32(
      0U, caps & static_cast<uint32_t>(rpc::RPC_CAPABILITY_SD));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_hal_weak_defaults_without_mock);
  return UNITY_END();
}