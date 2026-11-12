#pragma once

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include <PigweedAVR.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/string_view.h>
#include <pw_rpc/internal/packet.h>
#include <pw_span/span.h>
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

  pw::span<const uint8_t> span() const {
    return pw::span<const uint8_t>(data.data(), len);
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
  static size_t encodeUIFrame(uint64_t address, pw::span<const uint8_t> payload,
                              etl::array<uint8_t, N>& out_buf) {
    pw::stream::MemoryWriter writer(
        pw::as_writable_bytes(pw::span(out_buf.data(), out_buf.size())));
    auto res = pw::hdlc::WriteUIFrame(address, pw::as_bytes(payload), writer);
    return res.ok() ? writer.bytes_written() : 0;
  }
};

template <size_t N>
inline bool extract_next_valid_hdlc_frame(
    const ByteBuffer<N>& buffer, size_t& cursor,
    pw::hdlc::DecoderBuffer<1024>& decoder, pw::hdlc::Frame& out_frame) {
  while (cursor < buffer.len) {
    auto res = decoder.Process(static_cast<pw::byte>(buffer.data[cursor++]));
    if (res.ok()) {
      out_frame = res.value();
      return true;
    }
  }
  return false;
}

inline size_t build_rpc_packet(pw::rpc::internal::pwpb::PacketType type,
                               uint32_t service_id, uint32_t method_id,
                               pw::span<const uint8_t> payload,
                               uint8_t* out_buf, size_t out_max,
                               uint32_t call_id = 0, uint32_t channel_id = 1) {
  pw::rpc::internal::Packet packet(type, channel_id, service_id, method_id,
                                   call_id, pw::as_bytes(payload));
  auto res = packet.Encode(pw::as_writable_bytes(pw::span(out_buf, out_max)));
  return res.ok() ? res.value().size() : 0;
}

template <size_t N>
inline size_t build_hdlc_rpc_frame(uint64_t address,
                                   pw::rpc::internal::pwpb::PacketType type,
                                   uint32_t service_id, uint32_t method_id,
                                   pw::span<const uint8_t> payload,
                                   etl::array<uint8_t, N>& out_frame_buf,
                                   uint32_t call_id = 0,
                                   uint32_t channel_id = 1) {
  uint8_t packet_buf[512];
  size_t pkt_len =
      build_rpc_packet(type, service_id, method_id, payload, packet_buf,
                       sizeof(packet_buf), call_id, channel_id);
  if (pkt_len == 0) return 0;
  return TestHdlc::encodeUIFrame(
      address, pw::span<const uint8_t>(packet_buf, pkt_len), out_frame_buf);
}

// ---------------------------------------------------------------------------
// Canonical bridge reset helper – available when BRIDGE_ENABLE_TEST_INTERFACE
// is defined.
// ---------------------------------------------------------------------------

#ifdef BRIDGE_ENABLE_TEST_INTERFACE
#include "BridgeTestInterface.h"

inline void reset_bridge_core(BridgeClass& bridge, Stream& stream,
                              unsigned long baudrate = 0,
                              const char* secret = nullptr) {
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

inline size_t send_hdlc_rpc(bridge::test::TestAccessor& ba,
                            uint32_t service_hash, uint32_t method_hash,
                            pw::span<const uint8_t> payload = {},
                            pw::rpc::internal::pwpb::PacketType type =
                                pw::rpc::internal::pwpb::PacketType::REQUEST,
                            uint32_t call_id = 0, uint32_t channel_id = 1) {
  etl::array<uint8_t, 256> frame;
  size_t len = build_hdlc_rpc_frame(1, type, service_hash, method_hash, payload,
                                    frame, call_id, channel_id);
  if (len > 0) {
    ba.feedHdlcBytes(pw::span<const uint8_t>(frame.data(), len));
  }
  return len;
}

template <typename NanopbType, size_t BufCap = 64>
inline size_t send_rpc_request(bridge::test::TestAccessor& ba,
                               uint32_t service_hash, uint32_t method_hash,
                               const pb_msgdesc_t* fields,
                               const NanopbType& msg, uint32_t call_id = 0) {
  uint8_t payload[BufCap];
  pb_ostream_t os = pb_ostream_from_buffer(payload, sizeof(payload));
  if (!pb_encode(&os, fields, &msg)) return 0;
  return send_hdlc_rpc(ba, service_hash, method_hash,
                       pw::span<const uint8_t>(payload, os.bytes_written),
                       pw::rpc::internal::pwpb::PacketType::REQUEST, call_id);
}

template <typename NanopbType, size_t BufCap = 128>
inline void invoke_rpc_response(bridge::test::TestAccessor& ba,
                                uint32_t service_id, uint32_t method_id,
                                const pb_msgdesc_t* fields,
                                const NanopbType& msg, uint32_t call_id = 0,
                                uint32_t channel_id = 1) {
  uint8_t payload_buf[BufCap];
  pb_ostream_t pbos = pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
  if (pb_encode(&pbos, fields, &msg)) {
    pw::rpc::internal::Packet pkt(
        pw::rpc::internal::pwpb::PacketType::RESPONSE, channel_id, service_id,
        method_id, call_id,
        pw::as_bytes(pw::span(payload_buf, pbos.bytes_written)));
    ba.invokeRpcResponse(pkt);
  }
}

inline bool perform_test_link_sync(bridge::test::TestAccessor& ba,
                                   uint8_t nonce_fill = 0xABU,
                                   pw::span<uint8_t> out_nonce = {}) {
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> handshake_nonce = {};
  handshake_nonce.fill(nonce_fill);
  if (!out_nonce.empty()) {
    etl::copy_n(handshake_nonce.begin(),
                std::min(out_nonce.size(), handshake_nonce.size()),
                out_nonce.begin());
  }
  rpc_pb_LinkSync sync = rpc_pb_LinkSync_init_default;
  etl::copy_n(handshake_nonce.begin(), handshake_nonce.size(),
              sync.nonce.bytes);
  sync.nonce.size = handshake_nonce.size();
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> handshake_tag = {};
  ba.computeHandshakeTag(sync.nonce.bytes, sync.nonce.size,
                         handshake_tag.data());
  etl::copy_n(handshake_tag.begin(), handshake_tag.size(), sync.tag.bytes);
  sync.tag.size = handshake_tag.size();

  return send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                          pw::tokenizer::Hash("LinkSync"),
                          rpc_pb_LinkSync_fields, sync) > 0 &&
         ba.isSynchronized();
}

template <typename NanopbType>
inline bool decode_tx_response(const BiStream& stream,
                               const pb_msgdesc_t* fields,
                               NanopbType& out_msg) {
  pw::hdlc::DecoderBuffer<1024> decoder;
  for (size_t i = 0; i < stream.tx_buf.len; ++i) {
    auto frame = decoder.Process(static_cast<pw::byte>(stream.tx_buf.data[i]));
    if (!frame.ok()) continue;
    auto pkt = pw::rpc::internal::Packet::FromBuffer(frame.value().data());
    if (!pkt.ok() ||
        pkt.value().type() != pw::rpc::internal::pwpb::PacketType::RESPONSE) {
      continue;
    }
    auto payload = pkt.value().payload();
    pb_istream_t in = pb_istream_from_buffer(
        reinterpret_cast<const uint8_t*>(payload.data()), payload.size());
    return pb_decode(&in, fields, &out_msg);
  }
  return false;
}
#endif
