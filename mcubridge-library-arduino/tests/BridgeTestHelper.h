#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/mcubridge.pb.h"
#include "protocol/rpc_frame.h"
#include <pb_encode.h>

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const pb_msgdesc_t* fields, const T& msg) {
  pb_ostream_t stream = pb_ostream_from_buffer(
      frame.envelope.payload.bytes, 64U);
  if (pb_encode(&stream, fields, &msg)) {
    frame.envelope.payload.size = static_cast<pb_size_t>(stream.bytes_written);
  }
}

}  // namespace test
}  // namespace bridge

#endif
