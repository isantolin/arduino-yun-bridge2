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

namespace rpc {

inline constexpr size_t AEAD_NONCE_SIZE = rpc::RPC_AEAD_NONCE_SIZE;
inline constexpr size_t AEAD_TAG_SIZE = rpc::RPC_AEAD_TAG_SIZE;
inline constexpr size_t CRC_TRAILER_SIZE = rpc::RPC_CRC_SIZE;
inline constexpr size_t MAX_ENVELOPE_SIZE = rpc_pb_RpcEnvelope_size;
inline constexpr size_t MAX_FRAME_SIZE = MAX_ENVELOPE_SIZE + CRC_TRAILER_SIZE;

struct Frame {
  rpc_pb_RpcEnvelope envelope;
  uint32_t crc;

  Frame() : envelope(rpc_pb_RpcEnvelope_init_default), crc(0) {}

  Frame(const Frame& other) : envelope(other.envelope), crc(other.crc) {}

  Frame& operator=(const Frame& other) {
    if (this != &other) {
      envelope = other.envelope;
      crc = other.crc;
    }
    return *this;
  }

  etl::span<const uint8_t> payload() const {
    return etl::span<const uint8_t>(envelope.payload.bytes,
                                    envelope.payload.size);
  }
};

namespace checksum {
inline uint32_t compute(etl::span<const uint8_t> data) {
  etl::crc32 crc_gen;
  crc_gen.add(data.begin(), data.end());
  return crc_gen.value();
}
}  // namespace checksum

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
    if (buffer.size() < CRC_TRAILER_SIZE) return 0;

    pb_ostream_t stream =
        pb_ostream_from_buffer(buffer.data(), buffer.size() - CRC_TRAILER_SIZE);
    if (!rpc::Payload::encode(&stream, f.envelope)) return 0;

    const size_t encoded_size = stream.bytes_written;
    const uint32_t crc = checksum::compute(buffer.subspan(0, encoded_size));

    etl::byte_stream_writer writer(buffer.begin() + encoded_size,
                                   CRC_TRAILER_SIZE, etl::endian::little);
    writer.write<uint32_t>(crc);

    return encoded_size + CRC_TRAILER_SIZE;
  }

  static etl::expected<Frame, FrameError> parse(
      etl::span<const uint8_t> buffer) {
    if (buffer.size() < CRC_TRAILER_SIZE + 2U)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
    const uint32_t crc_calc = checksum::compute(buffer.subspan(0, crc_offset));

    uint32_t crc_received = 0;
    etl::byte_stream_reader reader(buffer.begin() + crc_offset,
                                   CRC_TRAILER_SIZE, etl::endian::little);
    crc_received = reader.read<uint32_t>().value_or(0U);

    if (crc_received != crc_calc)
      return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);

    Frame result;
    pb_istream_t stream = pb_istream_from_buffer(buffer.data(), crc_offset);
    if (!rpc::Payload::decode(&stream, result.envelope))
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    if (result.envelope.version != PROTOCOL_VERSION)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    result.crc = crc_calc;
    return result;
  }
};

class FrameBuilder {
 public:
  static size_t build(etl::span<uint8_t> buffer,
                      uint16_t seq_id, etl::span<const uint8_t> payload,
                      const etl::array<uint8_t, AEAD_NONCE_SIZE>& nonce,
                      const etl::array<uint8_t, AEAD_TAG_SIZE>& tag) {
    Frame f;
    f.envelope.version = PROTOCOL_VERSION;
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
