#include "Unity/src/unity.h"
#include "hal/hal.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"

bool g_host_has_sd = false;
bool g_host_fs_enabled = false;

void setUp() {}
void tearDown() {}

// Test weak defaults in hal.cpp when not overridden by a mock.

void test_hal_weak_defaults_without_mock() {
  const bool original_sd = g_host_has_sd;
  const bool original_fs = g_host_fs_enabled;

  g_host_has_sd = false;
  g_host_fs_enabled = false;

  TEST_ASSERT_FALSE(bridge::hal::hasSD());
  TEST_ASSERT_FALSE(bridge::hal::hasSPI());

  const auto write_res =
      bridge::hal::writeFile("test.txt", etl::span<const uint8_t>());
  TEST_ASSERT_FALSE(write_res.has_value());
  TEST_ASSERT_EQUAL(static_cast<int>(bridge::hal::HalError::NOT_IMPLEMENTED),
                    static_cast<int>(write_res.error()));

  uint8_t buffer[8];
  const auto read_res =
      bridge::hal::readFileChunk("test.txt", 0, etl::span<uint8_t>(buffer, 8));
  TEST_ASSERT_FALSE(read_res.has_value());
  TEST_ASSERT_EQUAL(static_cast<int>(bridge::hal::HalError::NOT_IMPLEMENTED),
                    static_cast<int>(read_res.error()));

  const auto remove_res = bridge::hal::removeFile("test.txt");
  TEST_ASSERT_FALSE(remove_res.has_value());
  TEST_ASSERT_EQUAL(static_cast<int>(bridge::hal::HalError::NOT_IMPLEMENTED),
                    static_cast<int>(remove_res.error()));

  rpc_pb_Capabilities caps = rpc_pb_Capabilities_init_default;
  bridge::hal::fillCapabilities(caps);
  TEST_ASSERT_FALSE(caps.sd);

  g_host_has_sd = original_sd;
  g_host_fs_enabled = original_fs;
}

#if defined(__STDC_HOSTED__) && (__STDC_HOSTED__ == 1)
int main() {
  UNITY_BEGIN();
  RUN_TEST(test_hal_weak_defaults_without_mock);
  return UNITY_END();
}
#endif
