#include "BridgeFaultInjection.h"
#include "hal/hal.h"
#include "hal/hal_internal.h"

// Defines the global Bridge instance as a TestAccessor so that test code can
// access protected members of BridgeClass via TestAccessor::create().
// Replaces BridgeInstance.cpp in test builds; never compiled in production
// firmware.

#define BRIDGE_NO_GLOBAL_EXTERN
#include "BridgeTestInterface.h"
#undef BRIDGE_NO_GLOBAL_EXTERN

static_assert(sizeof(bridge::test::TestAccessor) == sizeof(BridgeClass),
              "TestAccessor must be layout-compatible with BridgeClass");

bridge::test::TestAccessor Bridge(Serial);

// Host-only fault injection seam for POST failure tests; delegates to the
// canonical production implementation when no fault is armed.
namespace bridge::hal {

bool run_power_on_self_tests() {
  if (bridge::test::fault::consume(
          bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL)) {
    return false;
  }
  return internal::run_power_on_self_tests_impl();
}

}  // namespace bridge::hal
