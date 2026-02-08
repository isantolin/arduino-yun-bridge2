#ifndef ARDUINO_STRING_UTILS_H
#define ARDUINO_STRING_UTILS_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <etl/vector.h>
#include <etl/algorithm.h>
#include <etl/string_view.h>

#if defined(ARDUINO_ARCH_AVR)
extern "C" char __heap_start;
extern "C" char* __brkval;
#endif

/**
 * @brief Get free RAM (AVR specific).
 * @return Bytes free or 0 on non-AVR.
 */
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

/**
 * @brief Append a length-prefixed string (Pascal-style) to an ETL vector payload.
 * 
 * Uses etl::string_view for safety and flexibility.
 *
 * @param payload Destination etl::ivector (capacity agnostic).
 * @param str Source string view.
 */
inline void append_length_prefixed(
    etl::ivector<uint8_t>& payload,
    etl::string_view str) {
  size_t len = str.length();
  if (len > 255) {
      len = 255;
  }
  payload.push_back(static_cast<uint8_t>(len));
  if (len > 0) {
      payload.insert(payload.end(), str.begin(), str.begin() + len);
  }
}

/**
 * @brief Append a length-prefixed C string to an ETL vector payload.
 *
 * Legacy/convenience overload.
 *
 * @param payload  Destination etl::ivector.
 * @param str  Null-terminated source string (or raw buffer).
 * @param len  Number of bytes to copy.
 */
inline void append_length_prefixed(
    etl::ivector<uint8_t>& payload,
    const char* str,
    size_t len) {
  if (str == nullptr) {
    append_length_prefixed(payload, etl::string_view());
  } else {
    append_length_prefixed(payload, etl::string_view(str, len));
  }
}

#endif
