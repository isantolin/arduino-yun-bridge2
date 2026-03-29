#pragma once

#include <stdint.h>
#include <stddef.h>
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
#ifndef memcmp_P
#define memcmp_P(s1, s2, n) memcmp((s1), (s2), (n))
#endif
#endif

namespace bridge::hal {

/**
 * @brief Zero-cost abstraction for reading from Flash (PROGMEM) or RAM.
 * 
 * On AVR, it uses pgm_read_byte. On other architectures, it's a direct read.
 */
inline uint8_t read_byte(const uint8_t* addr) {
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
  if (n == 0) return;
  if constexpr (bridge::config::IS_AVR) {
#if defined(ARDUINO_ARCH_AVR) && !defined(BRIDGE_HOST_TEST)
    strncpy_P(dest, src, n);
#else
    strncpy(dest, src, n);
#endif
  } else {
    strncpy(dest, src, n);
  }
}

/**
 * @brief Safely copy typed data from PROGMEM or RAM.
 */
template <typename T>
inline void copy_from_progmem(T* dest, const T* src) {
  if constexpr (bridge::config::IS_AVR) {
#if defined(ARDUINO_ARCH_AVR) && !defined(BRIDGE_HOST_TEST)
    memcpy_P(dest, src, sizeof(T));
#else
    *dest = *src;
#endif
  } else {
    *dest = *src;
  }
}

} // namespace bridge::hal
