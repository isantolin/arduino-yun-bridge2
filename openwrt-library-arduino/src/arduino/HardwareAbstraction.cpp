#include "HardwareAbstraction.h"
#include "../Bridge.h" // For configuration macros

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#include <avr/pgmspace.h>
#endif

namespace bridge {
namespace hardware {

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

void initWatchdog() {
#if defined(ARDUINO_ARCH_AVR) && defined(BRIDGE_ENABLE_WATCHDOG) && BRIDGE_ENABLE_WATCHDOG
  wdt_enable(BRIDGE_WATCHDOG_TIMEOUT);
#endif
}

void resetWatchdog() {
#if defined(ARDUINO_ARCH_AVR) && defined(BRIDGE_ENABLE_WATCHDOG) && BRIDGE_ENABLE_WATCHDOG
  wdt_reset();
#endif
}

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) {
    free_bytes = 0;
  }
  if (static_cast<size_t>(free_bytes) > 0xFFFF) {
    free_bytes = 0xFFFF;
  }
  return static_cast<uint16_t>(free_bytes);
#else
  return 0;
#endif
}

} // namespace hardware
} // namespace bridge
