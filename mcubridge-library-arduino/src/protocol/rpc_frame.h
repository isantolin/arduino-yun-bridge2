/**
 * @file rpc_frame.h
 * @brief Zero-Wrapper Protobuf Framing (SIL-2).
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

namespace checksum {
inline uint32_t compute(etl::span<const uint8_t> data) {
  return etl::crc32_t16(data.begin(), data.end());
}
}  // namespace checksum


inline size_t serialize_frame(const rpc_pb_RpcEnvelope& env,
                              etl::span<uint8_t> buffer) {
  if (buffer.size() < CRC_TRAILER_SIZE) return 0;
  pb_ostream_t mem_stream =
      pb_ostream_from_buffer(buffer.data(), buffer.size() - CRC_TRAILER_SIZE);
  if (!pb_encode(&mem_stream, rpc_pb_RpcEnvelope_fields, &env)) return 0;
  const size_t encoded_size = mem_stream.bytes_written;
  const uint32_t crc = checksum::compute(buffer.subspan(0, encoded_size));
  buffer[encoded_size] = static_cast<uint8_t>(crc & 0xFF);
  buffer[encoded_size + 1] = static_cast<uint8_t>((crc >> 8) & 0xFF);
  buffer[encoded_size + 2] = static_cast<uint8_t>((crc >> 16) & 0xFF);
  buffer[encoded_size + 3] = static_cast<uint8_t>((crc >> 24) & 0xFF);
  return encoded_size + CRC_TRAILER_SIZE;
}

etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(
    etl::span<const uint8_t> buffer);

}  // namespace rpc
#endif
