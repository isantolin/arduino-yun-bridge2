/**
 * @file rpc_frame.h
 * @brief Zero-Wrapper Protobuf Framing (SIL-2).
 *
 * This file implements the wire-format framing using Nanopb directly.
 * All manual wrappers and redundant abstractions have been erradicated.
 */
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

inline bool is_compressed(uint16_t id) {
  return (id & RPC_CMD_FLAG_COMPRESSED) != 0;
}

namespace checksum {
/**
 * @brief Computes CRC32 using ETL directly (SIL-2).
 */
inline uint32_t compute(etl::span<const uint8_t> data) {
  return etl::crc32(data.begin(), data.end());
}
}  // namespace checksum

namespace Payload {

/**
 * @brief Decodes a specific payload type from an envelope.
 */
template <typename T>
inline etl::expected<T, FrameError> parse(const rpc_pb_RpcEnvelope& envelope) {
  T msg = {};
  pb_istream_t stream = pb_istream_from_buffer(envelope.payload.bytes,
                                               envelope.payload.size);
  if (!rpc::Payload::decode(&stream, msg)) {
    return etl::unexpected<FrameError>(FrameError::MALFORMED);
  }
  return etl::expected<T, FrameError>(msg);
}

}  // namespace Payload

/**
 * @brief Serializes an envelope directly to buffer with CRC (Zero-Wrapper).
 */
inline size_t serialize_frame(const rpc_pb_RpcEnvelope& env, etl::span<uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE) return 0;

  pb_ostream_t stream =
      pb_ostream_from_buffer(buffer.data(), buffer.size() - CRC_TRAILER_SIZE);
  if (!rpc::Payload::encode(&stream, env)) return 0;

  const size_t encoded_size = stream.bytes_written;
  const uint32_t crc = checksum::compute(buffer.subspan(0, encoded_size));

  etl::byte_stream_writer writer(buffer.begin() + encoded_size,
                                 CRC_TRAILER_SIZE, etl::endian::little);
  writer.write<uint32_t>(crc);

  return encoded_size + CRC_TRAILER_SIZE;
}

/**
 * @brief Parses a raw buffer into an envelope with CRC validation (Zero-Wrapper).
 */
inline etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(
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

  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  pb_istream_t stream = pb_istream_from_buffer(buffer.data(), crc_offset);
  if (!rpc::Payload::decode(&stream, env))
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  if (env.version != PROTOCOL_VERSION)
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  return env;
}

}  // namespace rpc

#endif
