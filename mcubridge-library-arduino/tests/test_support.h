#pragma once

#include <etl/algorithm.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "etl/crc32.h"
#include "unity.h"

static inline uint32_t crc32_ieee(const void* data, size_t len) {
  etl::crc32 crc_calc;
  crc_calc.add(reinterpret_cast<const uint8_t*>(data),
               reinterpret_cast<const uint8_t*>(data) + len);
  return crc_calc.value();
}

/* Legacy convenience macros – map to Unity assertions. */
#define TEST_ASSERT_EQ_UINT(actual, expected)     \
  TEST_ASSERT_EQUAL_UINT32((unsigned long)(expected), (unsigned long)(actual))

static inline void test_memfill(uint8_t* buf, size_t len, uint8_t value) {
  etl::fill_n(buf, len, value);
}

static inline int test_memeq(const void* a, const void* b, size_t len) {
  return memcmp(a, b, len) == 0;
}

template <size_t N>
struct ByteBuffer {
  uint8_t data[N];
  size_t len;
  size_t pos;

  ByteBuffer() : len(0), pos(0) { etl::fill_n(data, N, uint8_t{0}); }

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

  bool append(const uint8_t* src, size_t n) {
    if (!src && n) {
      return false;
    }
    if (len + n > N) {
      return false;
    }
    etl::copy_n(src, n, data + len);
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

// ---------------------------------------------------------------------------
// Reusable mock Stream classes for test binaries.
// ---------------------------------------------------------------------------

/**
 * Tx-only capture stream – records writes, no readable data.
 */
class TxCaptureStream : public Stream {
 public:
  ByteBuffer<4096> tx;

  size_t write(uint8_t c) override {
    tx.push(c);
    return 1;
  }
  size_t write(const uint8_t* b, size_t s) override {
    tx.append(b, s);
    return s;
  }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}
};

/**
 * Bidirectional mock stream – captures writes and feeds reads via feed().
 */
class BiStream : public Stream {
 public:
  ByteBuffer<4096> rx_buf;
  ByteBuffer<4096> tx_buf;

  int available() override { return rx_buf.remaining(); }
  int read() override { return rx_buf.read_byte(); }
  int peek() override { return rx_buf.peek_byte(); }
  size_t write(uint8_t b) override {
    tx_buf.push(b);
    return 1;
  }
  size_t write(const uint8_t* b, size_t s) override {
    tx_buf.append(b, s);
    return s;
  }
  void flush() override {}

  void feed(const uint8_t* data, size_t len) { rx_buf.append(data, len); }
};
