#include "pw_unit_test/framework.h"

#include "hal/hal.h"
#include "protocol/mcu_wire.pb.h"

bool g_host_has_sd = false;
bool g_host_fs_enabled = false;

// Test weak defaults in hal.cpp when not overridden by a mock.
TEST(HalWeakDefaults, WithoutMock) {
  const bool original_sd = g_host_has_sd;
  const bool original_fs = g_host_fs_enabled;

  g_host_has_sd = false;
  g_host_fs_enabled = false;

  EXPECT_FALSE(bridge::hal::hasSD());

  const auto remove_res = bridge::hal::removeFile("test.txt");
  EXPECT_TRUE(remove_res.IsUnimplemented());

  rpc_pb_Capabilities caps = rpc_pb_Capabilities_init_default;
  bridge::hal::fillCapabilities(caps);
  EXPECT_FALSE(caps.sd);
  EXPECT_EQ(caps.dig, NUM_DIGITAL_PINS);
  EXPECT_EQ(caps.ana, NUM_ANALOG_INPUTS);

  const auto wm = bridge::hal::getStackWatermark();
  EXPECT_EQ(wm.used_bytes, 0U);
  EXPECT_EQ(wm.budget_bytes, 0U);

  g_host_has_sd = original_sd;
  g_host_fs_enabled = original_fs;
}
