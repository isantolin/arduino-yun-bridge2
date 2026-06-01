#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc_pb_RpcEnvelope& frame, const T& msg) {
  pb_ostream_t stream = pb_ostream_from_buffer(
      frame.payload.bytes, 64U);
  if (rpc::Payload::encode(&stream, msg)) {
    frame.payload.size = static_cast<pb_size_t>(stream.bytes_written);
  }
}

}  // namespace test
}  // namespace bridge

#endif
