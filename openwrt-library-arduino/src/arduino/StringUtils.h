#ifndef ARDUINO_STRING_UTILS_H
#define ARDUINO_STRING_UTILS_H

#include <stddef.h>
#include <string.h>

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
