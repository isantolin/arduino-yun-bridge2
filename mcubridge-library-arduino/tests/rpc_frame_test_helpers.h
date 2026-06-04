/**
 * @file rpc_frame_test_helpers.h
 * @brief Test-only envelope construction helpers (NOT for production inclusion).
 */
#ifndef RPC_FRAME_TEST_HELPERS_H
#define RPC_FRAME_TEST_HELPERS_H

#include <etl/algorithm.h>
#include <etl/span.h>

#include "protocol/rpc_frame.h"

namespace rpc {

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

#endif  // RPC_FRAME_TEST_HELPERS_H
