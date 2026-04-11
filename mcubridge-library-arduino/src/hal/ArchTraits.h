#ifndef BRIDGE_ARCH_TRAITS_H
#define BRIDGE_ARCH_TRAITS_H

#include <stdint.h>
#include <Arduino.h>

#if defined(ARDUINO_ARCH_AVR)
#include <avr/wdt.h>
#elif defined(ARDUINO_ARCH_ESP32)
#include <esp_task_wdt.h>
#endif

namespace bridge::hal {

enum class ArchId : uint8_t {
  ARCH_ID_UNKNOWN = 0,
  ARCH_ID_AVR = 1,
  ARCH_ID_ESP32 = 2,
  ARCH_ID_SAMD = 3,
  ARCH_ID_HOST = 4
};

template <ArchId Id>
struct ArchTraits {
  static constexpr ArchId id = Id;
  static constexpr bool has_wdt = false;
  static constexpr bool is_harvard = false;
  static constexpr uint16_t default_free_memory = 0;
  static void reset() {}
};

#if defined(ARDUINO_ARCH_AVR)
template <>
struct ArchTraits<ArchId::ARCH_ID_AVR> {
  static constexpr ArchId id = ArchId::ARCH_ID_AVR;
  static constexpr bool has_wdt = true;
  static constexpr bool is_harvard = true;
  static constexpr uint16_t default_free_memory = 2048;
  static void reset() {
    wdt_enable(WDTO_15MS);
    // [SIL-2] Wait for hardware watchdog reset without raw loops.
    static etl::array<volatile uint8_t, 1> sentinel = {0};
    etl::for_each(sentinel.begin(), sentinel.end(), [](volatile uint8_t&){});
    reset(); // Recursive call until hardware reset occurs.
  }
};
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_ID_AVR
#elif defined(ARDUINO_ARCH_ESP32)
template <>
struct ArchTraits<ArchId::ARCH_ID_ESP32> {
  static constexpr ArchId id = ArchId::ARCH_ID_ESP32;
  static constexpr bool has_wdt = true;
  static constexpr bool is_harvard = false;
  static constexpr uint16_t default_free_memory = 320000;
  static void reset() { ESP.restart(); }
};
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_ID_ESP32
#elif defined(BRIDGE_HOST_TEST)
template <>
struct ArchTraits<ArchId::ARCH_ID_HOST> {
  static constexpr ArchId id = ArchId::ARCH_ID_HOST;
  static constexpr bool has_wdt = false;
  static constexpr bool is_harvard = false;
  static constexpr uint16_t default_free_memory = 65535;
  static void reset() {}
};
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_ID_HOST
#else
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_ID_UNKNOWN
#endif

using CurrentArchTraits = ArchTraits<BRIDGE_CURRENT_ARCH_ID>;

} // namespace bridge::hal

#endif
