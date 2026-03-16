#include "hal.h"
#include "config/bridge_config.h"

#if defined(ARDUINO_ARCH_AVR)
#include <avr/io.h>
extern "C" {
  extern char *__brkval;
  extern char __heap_start;
}
#endif

namespace bridge {
namespace hal {

bool isValidPin(uint8_t pin) {
#if defined(BRIDGE_HOST_TEST)
  (void)pin;
  return true; // Always allow in host tests/emulator
#elif defined(NUM_DIGITAL_PINS)
  return pin < NUM_DIGITAL_PINS;
#else
  return pin <= bridge::config::FALLBACK_MAX_PIN;
#endif
}

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  int v;
  return static_cast<uint16_t>(reinterpret_cast<int>(&v) - (__brkval == 0 ? reinterpret_cast<int>(&__heap_start) : reinterpret_cast<int>(__brkval)));
#elif defined(ARDUINO_ARCH_ESP32)
  return (uint16_t)ESP.getFreeHeap();
#else
  return bridge::config::FALLBACK_FREE_MEMORY;
#endif
}

void init() {
#if defined(ARDUINO_ARCH_AVR)
  // Enable watchdog or other AVR-specific init
#endif
}

}  // namespace hal
}  // namespace bridge
