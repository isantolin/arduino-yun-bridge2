#include "hal.h"

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
  (void)pin;
#if defined(BRIDGE_HOST_TEST)
  return true; // Always allow in host tests/emulator
#elif defined(NUM_DIGITAL_PINS)
  return pin < NUM_DIGITAL_PINS;
#else
  return pin <= 32;
#endif
}

uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  int v;
  return (uint16_t)((int)&v - (__brkval == 0 ? (int)&__heap_start : (int)__brkval));
#elif defined(ARDUINO_ARCH_ESP32)
  return (uint16_t)ESP.getFreeHeap();
#else
  return 1024;
#endif
}

}  // namespace hal
}  // namespace bridge
