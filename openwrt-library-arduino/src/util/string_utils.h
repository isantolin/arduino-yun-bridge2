#ifndef ARDUINO_STRING_UTILS_H
#define ARDUINO_STRING_UTILS_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <etl/vector.h>

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
  if (free_bytes > UINT16_MAX) {
    free_bytes = UINT16_MAX;
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

/**
 * @brief Append a length-prefixed C string to an ETL vector payload.
 *
 * Pushes a 1-byte length header followed by the string bytes.
 * This pattern is used by DataStore (key/value) and FileSystem (path).
 *
 * @tparam N  Capacity of the destination vector.
 * @param payload  Destination vector to append to.
 * @param str  Null-terminated source string.
 * @param len  Number of bytes to copy (must fit in uint8_t).
 */
template <size_t N>
inline void append_length_prefixed(
    etl::vector<uint8_t, N>& payload,
    const char* str,
    size_t len) {
  payload.push_back(static_cast<uint8_t>(len));
  payload.insert(
      payload.end(),
      reinterpret_cast<const uint8_t*>(str),
      reinterpret_cast<const uint8_t*>(str) + len);
}

#endif
