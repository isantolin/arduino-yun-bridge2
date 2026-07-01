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
#include <etl/byte_stream.h>
#include <etl/crc32.h>
#include <etl/expected.h>
#include <etl/span.h>

#include "rpc_protocol.h"
#include "rpc_structs.h"

namespace rpc {

/// [SIL-2] Single source of truth for system/status command classification.
/// Replaces repeated inline range checks across send paths.
inline constexpr bool is_system_command(uint16_t cmd) {
  return (cmd >= RPC_STATUS_CODE_MIN && cmd <= RPC_STATUS_CODE_MAX) ||
         (cmd >= RPC_SYSTEM_COMMAND_MIN && cmd <= RPC_SYSTEM_COMMAND_MAX);
}

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
  etl::byte_stream_writer writer(buffer.subspan(encoded_size, CRC_TRAILER_SIZE),
                                 etl::endian::little);
  writer.write<uint32_t>(crc);
  return encoded_size + CRC_TRAILER_SIZE;
}

etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(
    etl::span<const uint8_t> buffer);

}  // namespace rpc
#endif
