#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg) {
  pb_ostream_t stream = pb_ostream_from_buffer(
      frame.envelope.pb_msg.payload.bytes, 64U);
  if (msg.encode(&stream)) {
    frame.envelope.pb_msg.payload.size = static_cast<pb_size_t>(stream.bytes_written);
  }
}

}  // namespace test
}  // namespace bridge

#endif
