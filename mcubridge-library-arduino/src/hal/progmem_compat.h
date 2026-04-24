#pragma once

#include <etl/algorithm.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "config/bridge_config.h"

#if defined(ARDUINO_ARCH_AVR) && !defined(BRIDGE_HOST_TEST)
#include <avr/pgmspace.h>
#else
#ifndef PGM_P
#define PGM_P const char*
#endif
#ifndef PROGMEM
#define PROGMEM
#endif
#ifndef PSTR
#define PSTR(s) (s)
#endif
#ifndef pgm_read_byte
#define pgm_read_byte(addr) (*reinterpret_cast<const uint8_t*>(addr))
#endif
#ifndef memcpy_P
#define memcpy_P(dest, src, n) memcpy((dest), (src), (n))
#endif
#endif

namespace bridge::hal {

/**
 * @brief Zero-cost abstraction for reading from Flash (PROGMEM) or RAM.
 *
 * On AVR, it uses pgm_read_byte. On other architectures, it's a direct read.
 */
[[maybe_unused]] inline uint8_t read_byte(const uint8_t* addr) {
  if constexpr (bridge::config::IS_AVR) {
    return pgm_read_byte(addr);
  } else {
    return *addr;
  }
}

/**
 * @brief Safely copy a string from PROGMEM or RAM into a buffer.
 *
 * @param dest The destination buffer.
 * @param src The source string (may be in Flash on AVR).
 * @param n Maximum number of bytes to copy.
 */
inline void copy_string(char* dest, const char* src, size_t n) {
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
    d = static_cast<char>(read_byte(reinterpret_cast<const uint8_t*>(ctx.s++)));
    if (d == '\0') ctx.done = true;
  });
}

}  // namespace bridge::hal
