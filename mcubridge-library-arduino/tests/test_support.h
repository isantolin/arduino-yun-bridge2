#pragma once

#include <PigweedAVR.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pb_decode.h"
#include "pb_encode.h"
#include "protocol/mcu_wire.pb.h"
#include "protocol/rpc_protocol.h"

template <size_t N>
struct ByteBuffer {
  etl::array<uint8_t, N> data;
  size_t len;
  size_t pos;

  ByteBuffer() : len(0), pos(0) { data.fill(0); }

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
    etl::copy_n(src, n, data.begin() + len);
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

  etl::span<const uint8_t> span() const {
    return etl::span<const uint8_t>(data.data(), len);
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
  void clear() {
    rx_buf.clear();
    tx_buf.clear();
  }
};

// ---------------------------------------------------------------------------
// Pigweed HDLC test frame encoder & decoder helpers
// ---------------------------------------------------------------------------

struct TestHdlc {
  template <size_t N>
  static size_t encodeUIFrame(uint64_t address,
                              etl::span<const uint8_t> payload,
                              etl::array<uint8_t, N>& out_buf) {
    pw::stream::MemoryWriter writer(
        pw::as_writable_bytes(pw::span(out_buf.data(), out_buf.size())));
    auto res = pw::hdlc::WriteUIFrame(
        address, pw::as_bytes(pw::span(payload.data(), payload.size())),
        writer);
    return res.ok() ? writer.bytes_written() : 0;
  }
};

template <size_t N>
inline bool extract_next_valid_hdlc_frame(
    const ByteBuffer<N>& buffer, size_t& cursor,
    pw::hdlc::DecoderBuffer<1024>& decoder, pw::hdlc::Frame& out_frame) {
  while (cursor < buffer.len) {
    auto res = decoder.Process(static_cast<std::byte>(buffer.data[cursor++]));
    if (res.ok()) {
      out_frame = res.value();
      return true;
    }
  }
  return false;
}

inline size_t build_rpc_packet(pw::rpc_avr::PacketType type,
                               uint32_t service_id, uint32_t method_id,
                               etl::span<const uint8_t> payload,
                               uint8_t* out_buf, size_t out_max,
                               uint32_t call_id = 0, uint32_t channel_id = 1) {
  pw::rpc_avr::RpcPacket packet;
  packet.type = type;
  packet.channel_id = channel_id;
  packet.service_id = service_id;
  packet.method_id = method_id;
  packet.call_id = call_id;
  packet.payload = pw::ConstByteSpan(
      reinterpret_cast<const std::byte*>(payload.data()), payload.size());
  auto res = packet.Encode(pw::as_writable_bytes(pw::span(out_buf, out_max)));
  return res.ok() ? res.size() : 0;
}

template <size_t N>
inline size_t build_hdlc_rpc_frame(uint64_t address,
                                   pw::rpc_avr::PacketType type,
                                   uint32_t service_id, uint32_t method_id,
                                   etl::span<const uint8_t> payload,
                                   etl::array<uint8_t, N>& out_frame_buf,
                                   uint32_t call_id = 0,
                                   uint32_t channel_id = 1) {
  uint8_t packet_buf[512];
  size_t pkt_len =
      build_rpc_packet(type, service_id, method_id, payload, packet_buf,
                       sizeof(packet_buf), call_id, channel_id);
  if (pkt_len == 0) return 0;
  return TestHdlc::encodeUIFrame(
      address, etl::span<const uint8_t>(packet_buf, pkt_len), out_frame_buf);
}

// ---------------------------------------------------------------------------
// Canonical bridge reset helper – available when BRIDGE_ENABLE_TEST_INTERFACE
// is defined.
// ---------------------------------------------------------------------------

#ifdef BRIDGE_ENABLE_TEST_INTERFACE
#include "BridgeTestInterface.h"

inline void reset_bridge_core(BridgeClass& bridge, Stream& stream,
                              unsigned long baudrate = 0,
                              const char* secret = "top-secret") {
  bridge.~BridgeClass();
  new (&bridge) bridge::test::TestAccessor(stream);
  if (baudrate) {
    bridge.begin(baudrate, secret);
  } else {
    bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, secret);
  }
  auto& ba = bridge::test::TestAccessor::create(bridge);
  ba.setIdle();
}
#endif
