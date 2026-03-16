#ifndef BRIDGE_PROGMEM_COMPAT_H
#define BRIDGE_PROGMEM_COMPAT_H

/**
 * @file progmem_compat.h
 * @brief Centralized PROGMEM portability shim.
 *
 * Include this header instead of duplicating #ifndef PROGMEM blocks.
 * Does NOT pull in <Arduino.h> to avoid macro conflicts (e.g. min/max).
 */

#ifdef ARDUINO_ARCH_AVR
#include <avr/pgmspace.h>
#else
#ifndef PROGMEM
#define PROGMEM
#endif
#ifndef pgm_read_byte
#define pgm_read_byte(addr) (*(const unsigned char*)(addr))
#endif
#ifndef pgm_read_dword
#define pgm_read_dword(addr) \
  (*reinterpret_cast<const uint32_t*>(addr))  // NOLINT
#endif
#ifndef memcpy_P
#define memcpy_P memcpy
#endif
#ifndef memcmp_P
#define memcmp_P memcmp
#endif
#endif

#endif  // BRIDGE_PROGMEM_COMPAT_H
