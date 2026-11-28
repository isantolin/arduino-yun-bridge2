#include "config/bridge_config.h"

#if defined(ARDUINO_ARCH_ESP32) || defined(ARDUINO_ARCH_SAMD)
#if __has_include("pw_peripheral_backends/digital_io.cpp")
#include "pw_peripheral_backends/digital_io.cpp"
#endif
#if BRIDGE_ENABLE_SPI && __has_include("pw_peripheral_backends/initiator.cpp")
#include "pw_peripheral_backends/initiator.cpp"
#endif
#endif
