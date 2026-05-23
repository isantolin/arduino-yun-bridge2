#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include "protocol/rpc_frame.h"
#include "protocol/rpc_structs.h"

namespace bridge {
namespace test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg) {
  uint16_t command_id = frame.header.command_id & ~rpc::RPC_CMD_FLAG_COMPRESSED;
  rpc_pb_McuFrame mcu_frame = rpc_pb_McuFrame_init_default;
  mcu_frame.seq_id = frame.header.sequence_id;
  mcu_frame.which_message = command_id;
  memcpy(&mcu_frame.message, &msg, sizeof(msg)); // Hack for tests to map union

  pb_ostream_t stream = pb_ostream_from_buffer(
      const_cast<uint8_t*>(frame.payload.data()), rpc::MAX_PAYLOAD_SIZE);
  if (pb_encode(&stream, rpc_pb_McuFrame_fields, &mcu_frame)) {
    frame.header.payload_length = static_cast<uint16_t>(stream.bytes_written);
    frame.payload = frame.payload.subspan(0, stream.bytes_written);
  }
}

}  // namespace test
}  // namespace bridge

#endif
