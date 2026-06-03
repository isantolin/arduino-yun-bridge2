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
#include <etl/expected.h>
#include <etl/span.h>

#include "rpc_protocol.h"
#include "rpc_structs.h"

namespace rpc {

inline constexpr size_t AEAD_NONCE_SIZE = rpc::RPC_AEAD_NONCE_SIZE;
inline constexpr size_t AEAD_TAG_SIZE = rpc::RPC_AEAD_TAG_SIZE;
inline constexpr size_t MAX_ENVELOPE_SIZE = rpc_pb_RpcEnvelope_size;
inline constexpr size_t MAX_FRAME_SIZE = MAX_ENVELOPE_SIZE;

inline bool is_compressed(uint16_t id) {
  return (id & RPC_CMD_FLAG_COMPRESSED) != 0;
}


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
  pb_ostream_t stream = pb_ostream_from_buffer(buffer.data(), buffer.size());
  if (!rpc::Payload::encode(&stream, env)) return 0;
  return stream.bytes_written;
}

/**
 * @brief Parses a raw buffer into an envelope with CRC validation (Zero-Wrapper).
 */
inline etl::expected<rpc_pb_RpcEnvelope, FrameError> parse_frame(
    etl::span<const uint8_t> buffer) {
  if (buffer.size() < 2U)
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  pb_istream_t stream = pb_istream_from_buffer(buffer.data(), buffer.size());
  if (!rpc::Payload::decode(&stream, env))
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  if (env.version != PROTOCOL_VERSION)
    return etl::unexpected<FrameError>(FrameError::MALFORMED);

  return env;
}


/**
 * @brief Helper for building envelopes (primarily for tests/internal use).
 */
inline rpc_pb_RpcEnvelope build_envelope(uint16_t cmd_id, uint16_t seq_id, 
                                        etl::span<const uint8_t> payload = {},
                                        etl::span<const uint8_t> nonce = {},
                                        etl::span<const uint8_t> tag = {}) {
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = PROTOCOL_VERSION;
  env.command_id = cmd_id;
  env.sequence_id = seq_id;
  
  if (!nonce.empty()) {
    const size_t n_size = etl::min(nonce.size(), static_cast<size_t>(AEAD_NONCE_SIZE));
    etl::copy_n(nonce.begin(), n_size, env.nonce.bytes);
    env.nonce.size = static_cast<pb_size_t>(n_size);
  }
  
  if (!tag.empty()) {
    const size_t t_size = etl::min(tag.size(), static_cast<size_t>(AEAD_TAG_SIZE));
    etl::copy_n(tag.begin(), t_size, env.tag.bytes);
    env.tag.size = static_cast<pb_size_t>(t_size);
  }

  if (!payload.empty()) {
    const size_t p_size = etl::min(payload.size(), static_cast<size_t>(MAX_PAYLOAD_SIZE));
    etl::copy_n(payload.begin(), p_size, env.payload.bytes);
    env.payload.size = static_cast<pb_size_t>(p_size);
  }
  
  return env;
}

}  // namespace rpc

#endif
