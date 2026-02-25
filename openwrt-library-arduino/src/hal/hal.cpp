/**
 * @file hal.cpp
 * @brief HAL implementation for Arduino architectures.
 */
#include "hal.h"

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

namespace bridge {
namespace hal {

uint16_t getFreeMemory() {
#if defined(BRIDGE_HOST_TEST)
  return 4096; // Deterministic value for host tests
#elif defined(ARDUINO_ARCH_AVR)
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) return 0;
  if (free_bytes > UINT16_MAX) return UINT16_MAX;
  return static_cast<uint16_t>(free_bytes);
#else
  return 0;
#endif
}

bool isValidPin(uint8_t pin) {
#ifdef NUM_DIGITAL_PINS
  return pin < NUM_DIGITAL_PINS;
#else
  (void)pin;
  return true;
#endif
}

void init() {
  // Architecture specific initialization
}

} // namespace hal
} // namespace bridge
