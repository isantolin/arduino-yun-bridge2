// [SIL-2] Default global Bridge instance for production firmware.
// Excluded from host test builds, which substitute bridge_test_global.cpp.
#include "Bridge.h"

#if defined(SERIAL_PORT_LINUXBRIDGE)
BridgeClass Bridge(SERIAL_PORT_LINUXBRIDGE);
#elif defined(SERIAL_PORT_HARDWARE)
BridgeClass Bridge(SERIAL_PORT_HARDWARE);
#else
BridgeClass Bridge(Serial);
#endif
