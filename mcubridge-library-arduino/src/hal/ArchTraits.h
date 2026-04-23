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

#if defined(ARDUINO_ARCH_AVR) && !defined(BRIDGE_HOST_TEST)
#include <avr/pgmspace.h>
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_AVR
#define BRIDGE_PROGMEM PROGMEM
#define BRIDGE_PSTR(s) PSTR(s)
#elif defined(ARDUINO_ARCH_ESP32)
#include <esp_task_wdt.h>
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_ESP32
#define BRIDGE_PROGMEM
#define BRIDGE_PSTR(s) (s)
#elif defined(BRIDGE_HOST_TEST)
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_HOST
#define BRIDGE_PROGMEM
#define BRIDGE_PSTR(s) (s)
#else
#define BRIDGE_CURRENT_ARCH_ID ArchId::ARCH_UNKNOWN
#define BRIDGE_PROGMEM
#define BRIDGE_PSTR(s) (s)
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

  /**
   * @brief Zero-cost abstraction for reading from Flash (PROGMEM) or RAM.
   */
  static uint8_t read_byte(const uint8_t* addr) {
    if constexpr (Id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
      return pgm_read_byte(addr);
#else
      return *addr;
#endif
    } else {
      return *addr;
    }
  }

  /**
   * @brief Safely copy memory from Flash (PROGMEM) or RAM to RAM.
   */
  static void memcpy_to_ram(void* dest, const void* src, size_t n) {
    if constexpr (Id == ArchId::ARCH_AVR) {
#if defined(ARDUINO_ARCH_AVR)
      memcpy_P(dest, src, n);
#else
      memcpy(dest, src, n);
#endif
    } else {
      memcpy(dest, src, n);
    }
  }

  /**
   * @brief Safely copy a string from Flash (PROGMEM) or RAM into a RAM buffer.
   */
  static void copy_string(char* dest, const char* src, size_t n) {
    if (n == 0 || dest == nullptr || src == nullptr) return;
    struct {
      const char* s;
      bool done;
    } ctx = {src, false};
    etl::for_each(dest, dest + n, [&ctx](char& d) {
      if (ctx.done) {
        d = '\0';
        return;
      }
      d = static_cast<char>(
          read_byte(reinterpret_cast<const uint8_t*>(ctx.s++)));
      if (d == '\0') ctx.done = true;
    });
  }
};

using CurrentArchTraits = ArchTraits<BRIDGE_CURRENT_ARCH_ID>;

}  // namespace bridge::hal

#endif
