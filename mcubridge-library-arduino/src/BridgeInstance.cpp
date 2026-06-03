// [SIL-2] Default global Bridge instance for production firmware.
// Excluded from host test builds, which substitute bridge_test_global.cpp.
#include "Bridge.h"

BridgeClass Bridge(Serial);
