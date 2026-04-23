#ifndef BRIDGE_ARCH_TRAITS_H
#define BRIDGE_ARCH_TRAITS_H

#include <Arduino.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <stdint.h>

namespace bridge::hal {

enum class ArchId : uint8_t {
  ARCH_UNKNOWN = 0,
  ARCH_AVR = 1,
  ARCH_ESP32 = 2,
  ARCH_SAMD = 3,
  ARCH_HOST = 4
};

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_AVR
#elif defined(ARDUINO_ARCH_ESP32)
#include <esp_task_wdt.h>
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_ESP32
#elif defined(BRIDGE_HOST_TEST)
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_HOST
#else
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_UNKNOWN
#endif

template <ArchId Id>
struct ArchTraits {
  static constexpr ArchId id = Id;
  static constexpr bool has_wdt =
      (Id == ArchId::ARCH_AVR || Id == ArchId::ARCH_ESP32);
  static constexpr bool is_harvard = (Id == ArchId::ARCH_AVR);
  static constexpr uint32_t default_free_memory =
      (Id == ArchId::ARCH_AVR)     ? 2048
      : (Id == ArchId::ARCH_ESP32) ? 320000
                                   : 65535;

  static void reset() {
    if constexpr (Id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
      wdt_enable(WDTO_15MS);
      static etl::array<volatile uint8_t, 1> s = {0};
      etl::for_each(s.begin(), s.end(), [](volatile uint8_t&) {});
      reset();
#endif
    } else if constexpr (Id == ArchId::ARCH_ESP32) {
#if defined(ARDUINO_ARCH_ESP32)
      ESP.restart();
#endif
    }
  }
};

using CurrentArchTraits = ArchTraits<BRIDGE_CURRENT_ARCH_ID>;

}  // namespace bridge::hal

#endif
