#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <unity.h>
#include "hal/hal.h"

void setUp() {}
void tearDown() {}

void test_hal_defaults() {
  // Just cover the weak defaults if they exist
  bridge::hal::forceSafeState();
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_hal_defaults);
  return UNITY_END();
}
