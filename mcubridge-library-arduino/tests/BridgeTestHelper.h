#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc_pb_RpcEnvelope& frame, const T& msg) {
  frame.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_tag;
  pb_ostream_t stream = pb_ostream_from_buffer(
      frame.payload_type.encrypted_payload.bytes, 64U);
  if (pb_encode(&stream, rpc::Payload::get_fields<T>(), &msg)) {
    frame.payload_type.encrypted_payload.size = static_cast<pb_size_t>(stream.bytes_written);
  }
}

}  // namespace test
}  // namespace bridge

#endif
