#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

#include "rpc_protocol.h"

#undef min
#undef max
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/byte_stream.h>
#include <etl/crc32.h>
#include <etl/expected.h>
#include <etl/span.h>

namespace rpc {

inline constexpr size_t AEAD_NONCE_SIZE = rpc::RPC_AEAD_NONCE_SIZE;
inline constexpr size_t AEAD_TAG_SIZE = rpc::RPC_AEAD_TAG_SIZE;
inline constexpr size_t CRC_TRAILER_SIZE = rpc::RPC_CRC_SIZE;
inline constexpr size_t FRAME_HEADER_SIZE = rpc::RPC_CRC_COVERED_HEADER_SIZE;
inline constexpr size_t MIN_FRAME_SIZE = rpc::RPC_MIN_FRAME_SIZE;
inline constexpr size_t MAX_FRAME_SIZE =
    FRAME_HEADER_SIZE + AEAD_NONCE_SIZE + MAX_PAYLOAD_SIZE + AEAD_TAG_SIZE +
    CRC_TRAILER_SIZE;
inline constexpr size_t MAX_RAW_FRAME_SIZE = MAX_FRAME_SIZE;

#pragma pack(push, 1)
struct FrameHeader {
  uint8_t version;
  uint16_t payload_length;
  uint16_t command_id;
  uint16_t sequence_id;
};
#pragma pack(pop)

static_assert(sizeof(FrameHeader) == 7, "FrameHeader must be exactly 7 bytes");

struct Frame {
  FrameHeader header;
  etl::array<uint8_t, AEAD_NONCE_SIZE> nonce;
  etl::span<const uint8_t> payload;
  etl::array<uint8_t, AEAD_TAG_SIZE> tag;
  uint32_t crc;
};

enum class FrameError { NONE = 0, CRC_MISMATCH, MALFORMED, OVERFLOW, AUTH_FAIL };

[[maybe_unused]] inline constexpr bool is_compressed(uint16_t id) {
  return (id & RPC_CMD_FLAG_COMPRESSED) != 0;
}

namespace checksum {
inline void serialize_header(const FrameHeader& h, etl::span<uint8_t> buffer) {
  // [MEM-SAVE] Avoids byte_stream overhead by direct struct mapping.
  etl::copy_n(reinterpret_cast<const uint8_t*>(&h), sizeof(FrameHeader), buffer.data());
}

inline uint32_t compute(etl::span<const uint8_t> data) {
  etl::crc32 crc;
  crc.add(data.begin(), data.end());
  return crc.value();
}

inline uint32_t compute(const Frame& f) {
  // [MEM-SAVE] Cast direct mapping eliminates the need for temporary header_buf.
  etl::crc32 crc;
  crc.add(reinterpret_cast<const uint8_t*>(&f.header), reinterpret_cast<const uint8_t*>(&f.header) + sizeof(FrameHeader));
  crc.add(f.nonce.begin(), f.nonce.end());
  crc.add(f.payload.begin(), f.payload.end());
  crc.add(f.tag.begin(), f.tag.end());
  return crc.value();
}
}  // namespace checksum

class FrameParser {
 public:
  // [MEM-SAVE] Centralized serialization with optional AEAD reduces duplicated logic in BridgeClass.
  static size_t serialize(const Frame& f, etl::span<uint8_t> buffer) {
    const size_t required = FRAME_HEADER_SIZE + AEAD_NONCE_SIZE +
                            f.payload.size() + AEAD_TAG_SIZE +
                            CRC_TRAILER_SIZE;
    if (buffer.size() < required) return 0;

    checksum::serialize_header(f.header, buffer.subspan(0, FRAME_HEADER_SIZE));

    etl::copy(f.nonce.begin(), f.nonce.end(),
              buffer.begin() + FRAME_HEADER_SIZE);

    etl::copy_n(f.payload.data(), f.payload.size(),
                buffer.begin() + FRAME_HEADER_SIZE + AEAD_NONCE_SIZE);

    etl::copy(f.tag.begin(), f.tag.end(),
              buffer.begin() + FRAME_HEADER_SIZE + AEAD_NONCE_SIZE +
                  f.payload.size());

    etl::copy_n(reinterpret_cast<const uint8_t*>(&f.crc), CRC_TRAILER_SIZE,
                buffer.begin() + FRAME_HEADER_SIZE + AEAD_NONCE_SIZE +
                    f.payload.size() + AEAD_TAG_SIZE);
    return required;
  }

  // [MEM-SAVE] parse() now extracts and validates full frame metadata natively.
  static etl::expected<Frame, FrameError> parse(etl::span<const uint8_t> buffer) {
    if (buffer.size() < MIN_FRAME_SIZE || buffer.size() > MAX_RAW_FRAME_SIZE)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
    const uint32_t crc_calc =
        checksum::compute(buffer.subspan(0, crc_offset));

    Frame result = {};
    
    // [MEM-SAVE] reinterpret_cast mapping avoids field-by-field manual parsing.
    etl::copy_n(buffer.begin(), sizeof(FrameHeader), reinterpret_cast<uint8_t*>(&result.header));

    if (result.header.version != PROTOCOL_VERSION)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);
    if (buffer.size() !=
        (static_cast<size_t>(result.header.payload_length) + MIN_FRAME_SIZE))
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    // Read Nonce
    etl::copy_n(buffer.begin() + FRAME_HEADER_SIZE, AEAD_NONCE_SIZE,
                result.nonce.begin());

    // Read Payload (Encrypted)
    result.payload =
        buffer.subspan(FRAME_HEADER_SIZE + AEAD_NONCE_SIZE,
                       static_cast<size_t>(result.header.payload_length));

    // Read Tag
    etl::copy_n(buffer.begin() + FRAME_HEADER_SIZE + AEAD_NONCE_SIZE +
                    result.header.payload_length,
                AEAD_TAG_SIZE, result.tag.begin());

    uint32_t crc_opt = 0;
    etl::copy_n(buffer.begin() + crc_offset, CRC_TRAILER_SIZE, reinterpret_cast<uint8_t*>(&crc_opt));

    if (crc_opt != crc_calc)
      return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);
    result.crc = crc_calc;
    return result;
  }
};

class FrameBuilder {
 public:
  [[maybe_unused]] static size_t build(
      etl::span<uint8_t> buffer, uint16_t cmd_id, uint16_t seq_id,
      etl::span<const uint8_t> payload,
      const etl::array<uint8_t, AEAD_NONCE_SIZE>& nonce,
      const etl::array<uint8_t, AEAD_TAG_SIZE>& tag) {
    const size_t required = FRAME_HEADER_SIZE + AEAD_NONCE_SIZE +
                            payload.size() + AEAD_TAG_SIZE + CRC_TRAILER_SIZE;
    if (buffer.size() < required) return 0;
    Frame f = {};
    f.header.version = PROTOCOL_VERSION;
    f.header.payload_length = static_cast<uint16_t>(payload.size());
    f.header.command_id = cmd_id;
    f.header.sequence_id = seq_id;
    f.nonce = nonce;
    f.payload = payload;
    f.tag = tag;
    f.crc = checksum::compute(f);
    return FrameParser::serialize(f, buffer);
  }
};

}  // namespace rpc

#endif
