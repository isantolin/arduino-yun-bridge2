#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

#include "rpc_protocol.h"

// ETL requires min/max from <algorithm>, but Arduino.h defines them as macros.
#undef min
#undef max
#include <etl/byte_stream.h>
#include <etl/crc32.h>
#include <etl/expected.h>
#include <etl/span.h>

namespace rpc {

inline constexpr size_t CRC_TRAILER_SIZE = sizeof(uint32_t);

// --- Protocol Offset Constants [SIL-2] ---
inline constexpr size_t FRAME_HEADER_SIZE = 7;
inline constexpr size_t MIN_FRAME_SIZE = FRAME_HEADER_SIZE + CRC_TRAILER_SIZE;

// Define FrameHeader struct before it is used in sizeof()
#pragma pack(push, 1)
struct FrameHeader {
  uint8_t version;
  uint16_t payload_length;
  uint16_t command_id;
  uint16_t sequence_id;
};
#pragma pack(pop)

static_assert(sizeof(FrameHeader) == 7, "FrameHeader must be exactly 7 bytes");

// Maximum size of a raw frame (Header + Payload + CRC)
inline constexpr size_t MAX_RAW_FRAME_SIZE =
    sizeof(FrameHeader) + MAX_PAYLOAD_SIZE + CRC_TRAILER_SIZE;

struct Frame {
  FrameHeader header;
  etl::span<const uint8_t> payload;
  uint32_t crc;
};

/**
 * @brief Parse error codes for FrameParser.
 */
enum class FrameError {
  NONE = 0,
  CRC_MISMATCH,
  MALFORMED,
  OVERFLOW
};

class FrameParser {
 public:
  FrameParser() = default;

  etl::expected<Frame, FrameError> parse(etl::span<const uint8_t> buffer) {
    if (buffer.size() < MIN_FRAME_SIZE || buffer.size() > MAX_RAW_FRAME_SIZE)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    // [SIL-2] Big-Endian Strict Stream Reader
    etl::byte_stream_reader reader(buffer.begin(), buffer.end(), etl::endian::big);

    const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
    etl::crc32 crc_calc;
    crc_calc.add(buffer.begin(), buffer.begin() + crc_offset);

    // Read header fields with atomic cursor advancement
    Frame result;
    auto version_opt = reader.read<uint8_t>();
    auto payload_len_opt = reader.read<uint16_t>();
    auto command_id_opt = reader.read<uint16_t>();
    auto sequence_id_opt = reader.read<uint16_t>();

    if (!version_opt || !payload_len_opt || !command_id_opt || !sequence_id_opt)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    result.header.version = *version_opt;
    result.header.payload_length = *payload_len_opt;
    result.header.command_id = *command_id_opt;
    result.header.sequence_id = *sequence_id_opt;

    if (result.header.version != PROTOCOL_VERSION)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    if (buffer.size() != (static_cast<size_t>(result.header.payload_length) + MIN_FRAME_SIZE))
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    if (result.header.payload_length > MAX_PAYLOAD_SIZE)
      return etl::unexpected<FrameError>(FrameError::OVERFLOW);

    // Capture payload span and validate CRC
    result.payload = buffer.subspan(FRAME_HEADER_SIZE, result.header.payload_length);
    
    reader.skip<uint8_t>(result.header.payload_length);
    auto crc_opt = reader.read<uint32_t>();
    
    if (!crc_opt || *crc_opt != crc_calc.value())
      return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);

    result.crc = crc_calc.value();
    return result;
  }
};

/**
 * @brief Big-Endian Read/Write Helpers [SIL-2]
 */
inline uint16_t read_u16_be(etl::span<const uint8_t> buffer) {
  etl::byte_stream_reader reader(buffer.begin(), buffer.end(), etl::endian::big);
  return reader.read<uint16_t>().value_or(0);
}

inline void write_u16_be(etl::span<uint8_t> buffer, uint16_t value) {
  etl::byte_stream_writer writer(buffer.begin(), buffer.end(), etl::endian::big);
  writer.write<uint16_t>(value);
}

inline uint32_t read_u32_be(etl::span<const uint8_t> buffer) {
  etl::byte_stream_reader reader(buffer.begin(), buffer.end(), etl::endian::big);
  return reader.read<uint32_t>().value_or(0);
}

inline void write_u32_be(etl::span<uint8_t> buffer, uint32_t value) {
  etl::byte_stream_writer writer(buffer.begin(), buffer.end(), etl::endian::big);
  writer.write<uint32_t>(value);
}

inline uint64_t read_u64_be(etl::span<const uint8_t> buffer) {
  etl::byte_stream_reader reader(buffer.begin(), buffer.end(), etl::endian::big);
  return reader.read<uint64_t>().value_or(0);
}

inline void write_u64_be(etl::span<uint8_t> buffer, uint64_t value) {
  etl::byte_stream_writer writer(buffer.begin(), buffer.end(), etl::endian::big);
  writer.write<uint64_t>(value);
}

class FrameBuilder {
 public:
  FrameBuilder() = default;

  static size_t build(etl::span<uint8_t> buffer, uint16_t command_id, uint16_t sequence_id,
                      etl::span<const uint8_t> payload) {
    if (payload.size() > MAX_PAYLOAD_SIZE) return 0;

    const uint16_t payload_len = static_cast<uint16_t>(payload.size());
    const size_t total_len = FRAME_HEADER_SIZE + payload_len + CRC_TRAILER_SIZE;
    if (total_len > buffer.size()) return 0;

    // [SIL-2] Big-Endian Strict Stream Writer
    etl::byte_stream_writer writer(buffer.begin(), buffer.end(), etl::endian::big);

    writer.write<uint8_t>(static_cast<uint8_t>(PROTOCOL_VERSION));
    writer.write<uint16_t>(payload_len);
    writer.write<uint16_t>(command_id);
    writer.write<uint16_t>(sequence_id);

    if (payload_len > 0) {
        writer.write(payload);
    }

    etl::crc32 crc_calc;
    crc_calc.add(buffer.begin(), buffer.begin() + (FRAME_HEADER_SIZE + payload_len));
    writer.write<uint32_t>(static_cast<uint32_t>(crc_calc.value()));

    return total_len;
  }
};

}  // namespace rpc

#endif  // RPC_FRAME_H
