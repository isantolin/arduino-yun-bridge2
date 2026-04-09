#ifndef BRIDGE_ARCH_TRAITS_H
#define BRIDGE_ARCH_TRAITS_H

#include <stdint.h>
#include <Arduino.h>

namespace bridge::hal {

enum class ArchId : uint8_t {
  UNKNOWN = 0,
  AVR = 1,
  ESP32 = 2,
  SAMD = 3,
  HOST = 4
};

template <ArchId Id>
struct ArchTraits {
  static constexpr ArchId id = Id;
  static constexpr bool has_wdt = false;
  static constexpr bool is_harvard = false;
  static constexpr uint16_t default_free_memory = 0;
};

#if defined(ARDUINO_ARCH_AVR)
template <>
struct ArchTraits<ArchId::AVR> {
  static constexpr ArchId id = ArchId::AVR;
  static constexpr bool has_wdt = true;
  static constexpr bool is_harvard = true;
  static constexpr uint16_t default_free_memory = 2048;
  static void reset() {
#include <avr/wdt.h>
    wdt_enable(WDTO_15MS);
    while(true);
  }
};
#define BRIDGE_CURRENT_ARCH ArchId::AVR
#elif defined(ARDUINO_ARCH_ESP32)
template <>
struct ArchTraits<ArchId::ESP32> {
  static constexpr ArchId id = ArchId::ESP32;
  static constexpr bool has_wdt = true;
  static constexpr bool is_harvard = false;
  static constexpr uint16_t default_free_memory = 320000;
  static void reset() { ESP.restart(); }
};
#define BRIDGE_CURRENT_ARCH ArchId::ESP32
#elif defined(BRIDGE_HOST_TEST)
template <>
struct ArchTraits<ArchId::HOST> {
  static constexpr ArchId id = ArchId::HOST;
  static constexpr bool has_wdt = false;
  static constexpr bool is_harvard = false;
  static constexpr uint16_t default_free_memory = 65535;
  static void reset() {}
};
#define BRIDGE_CURRENT_ARCH ArchId::HOST
#else
#define BRIDGE_CURRENT_ARCH ArchId::UNKNOWN
#endif

using CurrentArchTraits = ArchTraits<BRIDGE_CURRENT_ARCH>;

} // namespace bridge::hal

#endif
