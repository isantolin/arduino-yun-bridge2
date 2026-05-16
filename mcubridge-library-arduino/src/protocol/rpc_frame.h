#ifndef RPC_FRAME_H
#define RPC_FRAME_H

#include <Arduino.h>

#include "rpc_protocol.h"

#undef min
#undef max
#include <etl/algorithm.h>
#include <etl/byte_stream.h>
#include <etl/crc32.h>
#include <etl/expected.h>
#include <etl/crc32.h>
#include "rpc_protocol.h"

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

template <typename... Args>
inline constexpr bool is_any_of(uint16_t id, Args... args) {
  return ((id == static_cast<uint16_t>(args)) || ...);
}

[[maybe_unused]] inline constexpr bool is_compressed(uint16_t id) {
  return (id & RPC_CMD_FLAG_COMPRESSED) != 0;
}

namespace checksum {
inline void serialize_header(const FrameHeader& h, etl::span<uint8_t> buffer) {
  etl::byte_stream_writer writer(buffer.data(), buffer.size(),
                                 etl::endian::big);
  writer.write<uint8_t>(h.version);
  writer.write<uint16_t>(h.payload_length);
  writer.write<uint16_t>(h.command_id);
  writer.write<uint16_t>(h.sequence_id);
}

inline uint32_t compute(const Frame& f) {
  etl::crc32_t4 crc;
  etl::array<uint8_t, FRAME_HEADER_SIZE> header_buf;
  serialize_header(f.header, header_buf);
  crc.add(header_buf.begin(), header_buf.end());
  crc.add(f.nonce.begin(), f.nonce.end());
  crc.add(f.payload.begin(), f.payload.end());
  crc.add(f.tag.begin(), f.tag.end());
  return crc.value();
}
}  // namespace checksum

class FrameParser {
 public:
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

    etl::byte_stream_writer writer(
        buffer.data() + FRAME_HEADER_SIZE + AEAD_NONCE_SIZE + f.payload.size() +
            AEAD_TAG_SIZE,
        CRC_TRAILER_SIZE, etl::endian::big);
    writer.write<uint32_t>(f.crc);
    return required;
  }

  etl::expected<Frame, FrameError> parse(etl::span<const uint8_t> buffer) {
    if (buffer.size() < MIN_FRAME_SIZE || buffer.size() > MAX_RAW_FRAME_SIZE)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    etl::byte_stream_reader reader(buffer.data(), buffer.size(),
                                   etl::endian::big);
    const size_t crc_offset = buffer.size() - CRC_TRAILER_SIZE;
    etl::crc32_t4 crc_calc;
    crc_calc.add(buffer.begin(), buffer.begin() + crc_offset);

    Frame result = {};
    const auto v_opt = reader.read<uint8_t>();
    const auto l_opt = reader.read<uint16_t>();
    const auto c_opt = reader.read<uint16_t>();
    const auto s_opt = reader.read<uint16_t>();

    if (!v_opt || !l_opt || !c_opt || !s_opt)
      return etl::unexpected<FrameError>(FrameError::MALFORMED);

    result.header = {*v_opt, *l_opt, *c_opt, *s_opt};

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

    reader.skip<uint8_t>(AEAD_NONCE_SIZE + result.header.payload_length +
                         AEAD_TAG_SIZE);
    const auto crc_opt = reader.read<uint32_t>();

#if BRIDGE_HOST_TEST
    if (!crc_opt || *crc_opt != crc_calc.value()) {
      fprintf(stderr,
              "[PARSE] CRC MISMATCH! Size: %zu, Calc: %08X, Recv: %08X\n",
              buffer.size(), (unsigned int)crc_calc.value(),
              (unsigned int)(crc_opt ? *crc_opt : 0));
    }
#endif

    if (!crc_opt || *crc_opt != crc_calc.value())
      return etl::unexpected<FrameError>(FrameError::CRC_MISMATCH);
    result.crc = crc_calc.value();
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
