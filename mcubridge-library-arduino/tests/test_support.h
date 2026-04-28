#pragma once

#include <etl/algorithm.h>
#include <etl/crc32.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "unity.h"

static inline uint32_t crc32_ieee(const void* data, size_t len) {
  etl::crc32 crc_calc;
  crc_calc.add(reinterpret_cast<const uint8_t*>(data),
               reinterpret_cast<const uint8_t*>(data) + len);
  return crc_calc.value();
}

/* Legacy convenience macros – map to Unity assertions. */
#define TEST_ASSERT_EQ_UINT(actual, expected) \
  TEST_ASSERT_EQUAL_UINT32((unsigned long)(expected), (unsigned long)(actual))

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
    return static_cast<int>(data[pos++]);
  }

  int peek_byte() const {
    if (pos >= len) {
      return -1;
    }
    return static_cast<int>(data[pos]);
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
  ByteBuffer<8192> rx_buf;
  ByteBuffer<8192> tx_buf;

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
  void feed_frame(rpc::CommandId cmd, uint16_t seq, const etl::span<const uint8_t>& payload) {
    uint8_t raw[rpc::MAX_FRAME_SIZE];
    uint8_t encoded[rpc::MAX_FRAME_SIZE + 2];
    
    raw[0] = rpc::PROTOCOL_VERSION;
    etl::byte_stream_writer w(raw + 1, 6, etl::endian::big);
    w.write<uint16_t>(static_cast<uint16_t>(payload.size()));
    w.write<uint16_t>(rpc::to_underlying(cmd));
    w.write<uint16_t>(seq);
    
    if (!payload.empty()) {
        etl::copy_n(payload.data(), payload.size(), raw + rpc::FRAME_HEADER_SIZE);
    }
    
    etl::crc32 crc;
    crc.add(raw, raw + rpc::FRAME_HEADER_SIZE + payload.size());
    uint32_t cv = crc.value();
    etl::byte_stream_writer w_crc(raw + rpc::FRAME_HEADER_SIZE + payload.size(), 4, etl::endian::big);
    w_crc.write<uint32_t>(cv);
    
    size_t encoded_len = TestCOBS::encode(raw, rpc::FRAME_HEADER_SIZE + payload.size() + 4, encoded);
    feed(encoded, encoded_len);
    uint8_t delim = rpc::RPC_FRAME_DELIMITER;
    feed(&delim, 1);
  }
  void clear() {
    rx_buf.clear();
    tx_buf.clear();
  }
};

/**
 * Perform a full LinkSync handshake on the given bridge instance.
 */
static inline void simulate_handshake(BridgeClass& bridge, BiStream& stream) {
  // 1. Enter Startup (Stabilized)
  bridge._onStartupStabilized();

  // 2. Feed CMD_LINK_SYNC
  etl::array<uint8_t, 16> nonce;
  etl::fill(nonce.begin(), nonce.end(), 0xAA);
  stream.feed_frame(rpc::CommandId::CMD_LINK_SYNC, 1,
                    etl::span<const uint8_t>(nonce.data(), 16));

  // 3. Process handshake
  bridge.process();
}

// ---------------------------------------------------------------------------
// COBS encoder/decoder for building test frames.
// [SIL-2] Mirrors the PacketSerial2::COBS codec used in production.
// Validated by roundtrip tests in test_protocol.cpp and test_bridge_core.cpp.
// ---------------------------------------------------------------------------

struct TestCOBS {
  static size_t encode(const uint8_t* src, size_t len, uint8_t* dst) {
    uint8_t* start = dst;
    uint8_t* code_ptr = dst++;
    uint8_t code = 1;
    for (size_t i = 0; i < len; ++i) {
      if (src[i] == 0) {
        *code_ptr = code;
        code_ptr = dst++;
        code = 1;
      } else {
        *dst++ = src[i];
        if (++code == rpc::RPC_UINT8_MASK) {
          *code_ptr = code;
          code_ptr = dst++;
          code = 1;
        }
      }
    }
    *code_ptr = code;
    return static_cast<size_t>(dst - start);
  }

  static size_t decode(const uint8_t* source, size_t length,
                       uint8_t* destination) {
    const uint8_t* src = source;
    const uint8_t* end = source + length;
    uint8_t* out = destination;
    while (src < end) {
      uint8_t code = *src++;
      if (code == 0) return 0;
      for (uint8_t i = 1; i < code; ++i) {
        if (src < end) {
          *out++ = *src++;
        } else {
          break;
        }
      }
      if (code < 0xFF && src < end) {
        *out++ = 0;
      }
    }
    return static_cast<size_t>(out - destination);
  }
};

// ---------------------------------------------------------------------------
// Frame extraction helper – decodes COBS segments and validates CRC.
// ---------------------------------------------------------------------------

template <size_t N>
static bool extract_next_valid_frame(const ByteBuffer<N>& buffer,
                                     size_t& cursor, rpc::Frame& out_frame) {
  rpc::FrameParser parser;
  uint8_t decoded_buf[1024];

  while (cursor < buffer.len) {
    if (buffer.data[cursor] == rpc::RPC_FRAME_DELIMITER) {
      cursor++;
      continue;
    }

    size_t end = cursor;
    while (end < buffer.len && buffer.data[end] != rpc::RPC_FRAME_DELIMITER)
      end++;

    const size_t segment_len =
        (end < buffer.len) ? (end - cursor + 1) : (end - cursor);
    size_t decoded_len =
        TestCOBS::decode(&buffer.data[cursor], segment_len, decoded_buf);

    if (decoded_len >= rpc::MIN_FRAME_SIZE) {
      etl::crc32 calc;
      calc.reset();
      calc.add(decoded_buf,
               decoded_buf + (decoded_len - rpc::CRC_TRAILER_SIZE));
      uint32_t cv = calc.value();
      etl::byte_stream_writer w(
          decoded_buf + decoded_len - rpc::CRC_TRAILER_SIZE,
          rpc::CRC_TRAILER_SIZE, etl::endian::big);
      w.write<uint32_t>(cv);

      auto result =
          parser.parse(etl::span<const uint8_t>(decoded_buf, decoded_len));
      if (result) {
        out_frame = result.value();
        cursor = end;
        return true;
      }
    }
    cursor = end;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Canonical bridge reset helper – available for test binaries.
// ---------------------------------------------------------------------------

static inline void reset_bridge_core(BridgeClass& bridge, Stream& stream,
                                     unsigned long baudrate = 0,
                                     const char* secret = "top-secret") {
  bridge.~BridgeClass();
  new (&bridge) BridgeClass(stream);
  if (baudrate) {
    bridge.begin(baudrate, secret);
  } else {
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);
  }
  bridge._onStartupStabilized();
}
