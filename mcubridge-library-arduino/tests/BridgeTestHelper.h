#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg) {
  pb_ostream_t stream = pb_ostream_from_buffer(
      const_cast<uint8_t*>(frame.payload.data()), rpc::MAX_PAYLOAD_SIZE);
  if (msg.encode(&stream)) {
    frame.header.payload_length = static_cast<uint16_t>(stream.bytes_written);
    frame.payload = frame.payload.subspan(0, stream.bytes_written);
  }
}

}  // namespace test
}  // namespace bridge

#endif
