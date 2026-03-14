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
  return 4096;  // Deterministic value for host tests
#elif defined(ARDUINO_ARCH_AVR)
  char stack_top;
  const char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) return 0;
  if (free_bytes > UINT16_MAX) return UINT16_MAX;
  return static_cast<uint16_t>(free_bytes);
#else
  // [SIL-2] Return UINT16_MAX (indeterminate) rather than 0 (out of memory)
  // for architectures without a known free-memory introspection method.
  return UINT16_MAX;
#endif
}

bool isValidPin(uint8_t pin) {
#ifdef NUM_DIGITAL_PINS
  return pin < NUM_DIGITAL_PINS;
#else
  static_cast<void>(pin);
  return true;
#endif
}

void init() {
  // [EXTENSION POINT] Architecture-specific hardware initialization.
  // Override per-board when porting to new targets (e.g. watchdog, GPIO mux).
}

}  // namespace hal
}  // namespace bridge
