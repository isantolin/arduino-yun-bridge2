#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif

#include <Arduino.h>
#include <etl/byte_stream.h>
#include <etl/crc32.h>
#include <etl/expected.h>
#include <etl/span.h>

#include "rpc_protocol.h"
#include "rpc_structs.h"

namespace PacketSerial2 {
/**
 * @brief CRC32 policy using ETL (SIL-2).
 */
struct CRC32 {
  static constexpr size_t ByteSize = 4;
  etl::crc32 _engine;
  inline void reset() { _engine.reset(); }
  inline void add(uint8_t b) { _engine.add(b); }
  inline uint32_t value() const { return _engine.value(); }
};
}  // namespace PacketSerial2

namespace rpc {

inline constexpr size_t AEAD_NONCE_SIZE = rpc::RPC_AEAD_NONCE_SIZE;
inline constexpr size_t AEAD_TAG_SIZE = rpc::RPC_AEAD_TAG_SIZE;

inline constexpr size_t MAX_ENVELOPE_SIZE = rpc_pb_RpcEnvelope_size;
inline constexpr size_t MAX_FRAME_SIZE = MAX_ENVELOPE_SIZE;

inline bool is_compressed(uint16_t id) {
  return (id & RPC_CMD_FLAG_COMPRESSED) != 0;
}

struct Frame {
  rpc_pb_RpcEnvelope envelope;

  Frame() : envelope(rpc_pb_RpcEnvelope_init_default) {}

  etl::span<const uint8_t> payload() const {
    return etl::span<const uint8_t>(envelope.payload.bytes,
                                    envelope.payload.size);
  }
};



namespace Payload {

template <typename T>
inline etl::expected<T, FrameError> parse(const rpc::Frame& frame) {
  T msg = {};
  pb_istream_t stream = pb_istream_from_buffer(frame.payload().data(),
                                               frame.envelope.payload.size);
  if (!rpc::Payload::decode(&stream, msg)) {
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  }
  return etl::expected<T, FrameError>(msg);
}

}  // namespace Payload

class FrameParser {
 public:
  static size_t serialize(const Frame& f, etl::span<uint8_t> buffer) {
    pb_ostream_t stream = pb_ostream_from_buffer(buffer.data(), buffer.size());
    if (!rpc::Payload::encode(&stream, f.envelope)) return 0;
    return stream.bytes_written;
  }

  static etl::expected<Frame, FrameError> parse(
      etl::span<const uint8_t> buffer) {
    Frame result;
    pb_istream_t stream = pb_istream_from_buffer(buffer.data(), buffer.size());
    if (!rpc::Payload::decode(&stream, result.envelope))
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    if (result.envelope.version != PROTOCOL_VERSION)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    return result;
  }
};

class FrameBuilder {
 public:
  static size_t build(etl::span<uint8_t> buffer, uint16_t cmd_id,
                      uint16_t seq_id, etl::span<const uint8_t> payload,
                      const etl::array<uint8_t, AEAD_NONCE_SIZE>& nonce,
                      const etl::array<uint8_t, AEAD_TAG_SIZE>& tag) {
    Frame f;
    f.envelope.version = PROTOCOL_VERSION;
    f.envelope.command_id = cmd_id;
    f.envelope.sequence_id = seq_id;

    etl::copy_n(nonce.begin(), AEAD_NONCE_SIZE, f.envelope.nonce.bytes);
    f.envelope.nonce.size = static_cast<pb_size_t>(AEAD_NONCE_SIZE);

    etl::copy_n(tag.begin(), AEAD_TAG_SIZE, f.envelope.tag.bytes);
    f.envelope.tag.size = static_cast<pb_size_t>(AEAD_TAG_SIZE);

    const size_t pl_size = etl::min(payload.size(), static_cast<size_t>(64U));
    etl::copy_n(payload.begin(), pl_size, f.envelope.payload.bytes);
    f.envelope.payload.size = static_cast<pb_size_t>(pl_size);

    return FrameParser::serialize(f, buffer);
  }
};

}  // namespace rpc

#endif
