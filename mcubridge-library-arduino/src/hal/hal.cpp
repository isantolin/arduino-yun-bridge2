#include "hal.h"
#include "config/bridge_config.h"
#include "protocol/rpc_protocol.h"

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

bool hasSD() {
#if defined(BRIDGE_HOST_TEST)
  return true; // Mock SD card availability for tests
#else
  // For actual hardware, this would check SD.begin() or similar.
  // Since we don't want to force SD.h dependency here, we return false
  // unless specifically enabled by a build flag.
  return false;
#endif
}

bool writeFile(const char* path, etl::span<const uint8_t> data) {
#if defined(BRIDGE_HOST_TEST)
  (void)path; (void)data;
  return true; // Mock success for tests
#else
  (void)path; (void)data;
  return false;
#endif
}

uint32_t getCapabilities() {
  uint32_t caps = 0;
#if BRIDGE_ENABLE_WATCHDOG
  caps |= rpc::RPC_CAPABILITY_WATCHDOG;
#endif
#if BRIDGE_ENABLE_RLE
  caps |= rpc::RPC_CAPABILITY_RLE;
#endif
#if defined(ARDUINO_ARCH_AVR) && defined(SERIAL_PORT_HARDWARE1)
  caps |= rpc::RPC_CAPABILITY_HW_SERIAL1;
#endif
#if defined(BRIDGE_ENABLE_DAC)
  caps |= rpc::RPC_CAPABILITY_DAC;
#endif
  return caps;
}

void getPinCounts(uint8_t& digital, uint8_t& analog) {
#if defined(BRIDGE_HOST_TEST)
  digital = bridge::config::SAMD_DIGITAL_PINS;
  analog = bridge::config::SAMD_ANALOG_PINS;
#elif defined(ARDUINO_ARCH_AVR)
  digital = bridge::config::AVR_DIGITAL_PINS;
  analog = bridge::config::AVR_ANALOG_PINS;
#elif defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_SAM)
  digital = bridge::config::SAMD_DIGITAL_PINS;
  analog = bridge::config::SAMD_ANALOG_PINS;
#else
  digital = bridge::config::FALLBACK_MAX_PIN;
  analog = 0;
#endif
}

}  // namespace hal
}  // namespace bridge
