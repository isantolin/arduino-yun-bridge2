#ifndef ARDUINO_STRING_UTILS_H
#define ARDUINO_STRING_UTILS_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

inline uint16_t getFreeMemory() {
#if defined(ARDUINO_ARCH_AVR)
  char stack_top;
  char* heap_end = __brkval ? __brkval : &__heap_start;
  intptr_t free_bytes = &stack_top - heap_end;
  if (free_bytes < 0) {
    free_bytes = 0;
  }
  if (free_bytes > 65535) {
    free_bytes = 65535;
  }
  return static_cast<uint16_t>(free_bytes);
#else
  return 0;
#endif
}

struct BoundedStringInfo {
  size_t length;
  bool overflowed;
};

inline BoundedStringInfo measure_bounded_cstring(
    const char* str, size_t max_len) {
  if (!str || max_len == 0) {
    return {0, true};
  }
  size_t measured = strnlen(str, max_len + 1U);
  bool overflowed = measured > max_len;
  if (overflowed) {
    measured = max_len;
  }
  return {measured, overflowed};
}

#endif
