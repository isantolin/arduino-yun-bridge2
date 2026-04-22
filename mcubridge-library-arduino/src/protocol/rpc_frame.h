#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

#include "rpc_protocol.h"

#undef min
#undef max
#include <etl/byte_stream.h>
#include <etl/crc32.h>
#include <etl/expected.h>
#include <etl/span.h>

namespace rpc {

inline constexpr size_t CRC_TRAILER_SIZE = sizeof(uint32_t);
inline constexpr size_t FRAME_HEADER_SIZE = 7;
inline constexpr size_t MIN_FRAME_SIZE = FRAME_HEADER_SIZE + CRC_TRAILER_SIZE;
inline constexpr size_t MAX_FRAME_SIZE =
    FRAME_HEADER_SIZE + MAX_PAYLOAD_SIZE + CRC_TRAILER_SIZE;
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
  etl::span<const uint8_t> payload;
  uint32_t crc;
};

enum class FrameError { NONE = 0, CRC_MISMATCH, MALFORMED, OVERFLOW };

template <typename... Args>
inline constexpr bool is_any_of(uint16_t id, Args... args) {
  return ((id == static_cast<uint16_t>(args)) || ...);
}

inline constexpr bool is_reliable(uint16_t id) {
  return is_any_of(id, CommandId::CMD_ENTER_BOOTLOADER,
                   CommandId::CMD_SET_PIN_MODE, CommandId::CMD_DIGITAL_WRITE,
                   CommandId::CMD_ANALOG_WRITE, CommandId::CMD_CONSOLE_WRITE,
                   CommandId::CMD_DATASTORE_PUT, CommandId::CMD_MAILBOX_PUSH,
                   CommandId::CMD_FILE_WRITE, CommandId::CMD_SPI_BEGIN,
                   CommandId::CMD_SPI_END, CommandId::CMD_SPI_SET_CONFIG);
}

[[maybe_unused]] inline constexpr bool is_compressed(uint16_t id) {
  return (id & RPC_CMD_FLAG_COMPRESSED) != 0;
}

namespace checksum {
inline uint32_t compute(const Frame& f) {
  etl::crc32 crc;
  etl::array<uint8_t, 7> h;
  etl::byte_stream_writer writer(h.data(), h.size(), etl::endian::big);
  writer.write<uint8_t>(f.header.version);
  writer.write<uint16_t>(f.header.payload_length);
  writer.write<uint16_t>(f.header.command_id);
  writer.write<uint16_t>(f.header.sequence_id);

  crc.add(h.begin(), h.end());
  crc.add(f.payload.begin(), f.payload.end());
  return crc.value();
}
}  // namespace checksum
class FrameParser {
 public:
  static size_t serialize(const Frame& f, etl::span<uint8_t> buffer) {
    if (buffer.size() <
        (sizeof(FrameHeader) + f.payload.size() + CRC_TRAILER_SIZE))
      return 0;
    etl::byte_stream_writer writer(buffer.data(), buffer.size(),
                                   etl::endian::big);
    writer.write<uint8_t>(f.header.version);
    writer.write<uint16_t>(f.header.payload_length);
    writer.write<uint16_t>(f.header.command_id);
    writer.write<uint16_t>(f.header.sequence_id);
    writer.write_unchecked(f.payload.data(), f.payload.size());
    writer.write<uint32_t>(f.crc);
    return writer.size_bytes();
  }

  etl::expected<Frame, FrameError> parse(etl::span<const uint8_t> buffer) {
    if (buffer.size() < MIN_FRAME_SIZE || buffer.size() > MAX_RAW_FRAME_SIZE)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    etl::byte_stream_reader reader(buffer.data(), buffer.size(),
                                   etl::endian::big);
    const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
    etl::crc32 crc_calc;
    crc_calc.add(buffer.begin(), buffer.begin() + crc_offset);

    Frame result = {};
    auto v_opt = reader.read<uint8_t>();
    auto l_opt = reader.read<uint16_t>();
    auto c_opt = reader.read<uint16_t>();
    auto s_opt = reader.read<uint16_t>();

    if (!v_opt || !l_opt || !c_opt || !s_opt)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    result.header.version = *v_opt;
    result.header.payload_length = *l_opt;
    result.header.command_id = *c_opt;
    result.header.sequence_id = *s_opt;

    if (result.header.version != PROTOCOL_VERSION)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);
    if (buffer.size() !=
        (static_cast<size_t>(result.header.payload_length) + MIN_FRAME_SIZE))
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    result.payload =
        buffer.subspan(FRAME_HEADER_SIZE, result.header.payload_length);
    reader.skip<uint8_t>(result.header.payload_length);
    auto crc_opt = reader.read<uint32_t>();

    if (!crc_opt || *crc_opt != crc_calc.value())
      return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);
    result.crc = crc_calc.value();
    return result;
  }
};

class FrameBuilder {
 public:
  [[maybe_unused]] static size_t build(etl::span<uint8_t> buffer,
                                       uint16_t cmd_id, uint16_t seq_id,
                                       etl::span<const uint8_t> payload) {
    if (buffer.size() < (FRAME_HEADER_SIZE + payload.size() + CRC_TRAILER_SIZE))
      return 0;
    Frame f = {};
    f.header.version = PROTOCOL_VERSION;
    f.header.payload_length = static_cast<uint16_t>(payload.size());
    f.header.command_id = cmd_id;
    f.header.sequence_id = seq_id;
    f.payload = payload;
    f.crc = checksum::compute(f);
    return FrameParser::serialize(f, buffer);
  }
};

}  // namespace rpc

#endif
