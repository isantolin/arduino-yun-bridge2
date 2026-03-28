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
#define pgm_read_byte(addr) (*(const uint8_t*)(addr))
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

} // namespace bridge::hal
