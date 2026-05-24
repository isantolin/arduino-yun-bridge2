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
#include <etl/string_view.h>

#include <pb_decode.h>
#include <pb_encode.h>
#include "protocol/mcubridge.pb.h"
#include "rpc_protocol.h"

namespace rpc {

namespace payload {

template <typename PbBytesArray>
inline void copy_to_pb_bytes(PbBytesArray& dest, const uint8_t* src,
                             size_t src_size) {
  constexpr size_t dest_size = sizeof(dest.bytes) / sizeof(dest.bytes[0]);
  const size_t to_copy = (src_size <= dest_size) ? src_size : dest_size;
  dest.size = static_cast<pb_size_t>(to_copy);
  if (to_copy > 0U) {
    etl::copy_n(src, to_copy, dest.bytes);
  }
}

template <typename PbStringArray>
inline void copy_to_pb_string(PbStringArray& dest, etl::string_view src) {
  constexpr size_t dest_size = sizeof(dest) / sizeof(dest[0]);
  const size_t to_copy = etl::min(src.size(), dest_size - 1U);
  if (to_copy > 0U) {
    etl::copy_n(src.begin(), to_copy, dest);
  }
  dest[to_copy] = '\0';
}

}  // namespace payload

inline constexpr size_t MAX_ENVELOPE_SIZE = rpc_pb_RpcEnvelope_size;
inline constexpr size_t MAX_FRAME_SIZE = MAX_ENVELOPE_SIZE + CRC_SIZE;

inline bool is_compressed(uint16_t id) {
  return (id & CMD_FLAG_COMPRESSED) != 0;
}

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
inline etl::expected<T, FrameError> parse(const rpc::Frame& frame,
                                          const pb_msgdesc_t* fields) {
  T msg = {};
  pb_istream_t stream = pb_istream_from_buffer(
      frame.payload().data(), frame.envelope.payload.size);
  if (!pb_decode(&stream, fields, &msg)) {
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  }
  return etl::expected<T, FrameError>(msg);
}

}  // namespace Payload

class FrameParser {
 public:
  static size_t serialize(const Frame& f, etl::span<uint8_t> buffer) {
    if (buffer.size() < CRC_SIZE) return 0;

    pb_ostream_t stream =
        pb_ostream_from_buffer(buffer.data(), buffer.size() - CRC_SIZE);
    if (!pb_encode(&stream, rpc_pb_RpcEnvelope_fields, &f.envelope)) return 0;

    const size_t encoded_size = stream.bytes_written;
    const uint32_t crc = checksum::compute(buffer.subspan(0, encoded_size));

    etl::byte_stream_writer writer(buffer.begin() + encoded_size,
                                   CRC_SIZE, etl::endian::little);
    writer.write<uint32_t>(crc);

    return encoded_size + CRC_SIZE;
  }

  static etl::expected<Frame, FrameError> parse(
      etl::span<const uint8_t> buffer) {
    if (buffer.size() < CRC_SIZE + 2U)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    const size_t crc_offset = buffer.size() - CRC_SIZE;
    const uint32_t crc_calc = checksum::compute(buffer.subspan(0, crc_offset));

    uint32_t crc_received = 0;
    etl::byte_stream_reader reader(buffer.begin() + crc_offset,
                                   CRC_SIZE, etl::endian::little);
    crc_received = reader.read<uint32_t>().value_or(0U);

    if (crc_received != crc_calc)
      return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);

    Frame result;
    pb_istream_t stream = pb_istream_from_buffer(buffer.data(), crc_offset);
    if (!pb_decode(&stream, rpc_pb_RpcEnvelope_fields, &result.envelope))
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    if (result.envelope.version != PROTOCOL_VERSION)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    result.crc = crc_calc;
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
