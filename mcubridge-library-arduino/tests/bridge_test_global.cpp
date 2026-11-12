// Defines the global Bridge instance as a TestAccessor so that test code can
// access protected members of BridgeClass via TestAccessor::create().
// Replaces Bridge.cpp in test builds; never compiled in production firmware.

#define BRIDGE_NO_GLOBAL_EXTERN
#include "BridgeTestInterface.h"
#undef BRIDGE_NO_GLOBAL_EXTERN

static_assert(sizeof(bridge::test::TestAccessor) == sizeof(BridgeClass),
              "TestAccessor must be layout-compatible with BridgeClass");

bridge::test::TestAccessor Bridge(Serial);
