#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <FastCRC.h>

static inline uint32_t crc32_ieee(const void *data, size_t len) {
  FastCRC32 crc_calc;
  return crc_calc.crc32((const uint8_t*)data, (uint16_t)len);
}

#define TEST_ASSERT(cond)                                                      \
  do {                                                                         \
    if (!(cond)) {                                                             \
      fprintf(stderr, "[FATAL] Assertion failed at %s:%d: %s\n", __FILE__,   \
              __LINE__, #cond);                                                \
      abort();                                                                 \
    }                                                                          \
  } while (0)

#define TEST_ASSERT_EQ_UINT(actual, expected)                                  \
  do {                                                                         \
    const unsigned long _a = (unsigned long)(actual);                          \
    const unsigned long _e = (unsigned long)(expected);                        \
    if (_a != _e) {                                                            \
      fprintf(stderr,                                                          \
              "[FATAL] Assertion failed at %s:%d: %s == %s (got %lu, exp %lu)\n", \
              __FILE__, __LINE__, #actual, #expected, _a, _e);                 \
      abort();                                                                 \
    }                                                                          \
  } while (0)

static inline void test_memfill(uint8_t *buf, size_t len, uint8_t value) {
  for (size_t i = 0; i < len; ++i) {
    buf[i] = value;
  }
}

static inline int test_memeq(const void *a, const void *b, size_t len) {
  return memcmp(a, b, len) == 0;
}

template <size_t N> struct ByteBuffer {
  uint8_t data[N];
  size_t len;
  size_t pos;

  ByteBuffer() : len(0), pos(0) { memset(data, 0, sizeof(data)); }

  void clear() {
    len = 0;
    pos = 0;
  }

  size_t remaining() const { return (pos <= len) ? (len - pos) : 0; }

  bool push(uint8_t b) {
    if (len >= N) {
      return false;
    }
    data[len++] = b;
    return true;
  }

  bool append(const uint8_t *src, size_t n) {
    if (!src && n) {
      return false;
    }
    if (len + n > N) {
      return false;
    }
    memcpy(data + len, src, n);
    len += n;
    return true;
  }

  int read_byte() {
    if (pos >= len) {
      return -1;
    }
    return (int)data[pos++];
  }

  int peek_byte() const {
    if (pos >= len) {
      return -1;
    }
    return (int)data[pos];
  }
};
