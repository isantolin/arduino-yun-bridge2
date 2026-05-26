#ifndef BRIDGE_TEST_HELPER_H
#define BRIDGE_TEST_HELPER_H

#include <pb_encode.h>
#include <pb_decode.h>
#include "protocol/mcubridge.pb.h"
#include "protocol/rpc_frame.h"

namespace bridge::test {

template <typename T>
void set_pb_payload(rpc::Frame& frame, const T& msg, uint32_t tag) {
  rpc_pb_RpcPayload payload = rpc_pb_RpcPayload_init_default;
  payload.which_msg = static_cast<pb_size_t>(tag);
  
  if (tag == rpc_pb_RpcPayload_digital_write_tag) {
    payload.msg.digital_write = *reinterpret_cast<const rpc_pb_DigitalWrite*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_link_sync_tag) {
    payload.msg.link_sync = *reinterpret_cast<const rpc_pb_LinkSync*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_set_pin_mode_tag) {
    payload.msg.set_pin_mode = *reinterpret_cast<const rpc_pb_PinMode*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_analog_write_tag) {
    payload.msg.analog_write = *reinterpret_cast<const rpc_pb_AnalogWrite*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_digital_read_tag) {
    payload.msg.digital_read = *reinterpret_cast<const rpc_pb_PinRead*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_analog_read_tag) {
    payload.msg.analog_read = *reinterpret_cast<const rpc_pb_PinRead*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_console_write_tag) {
    payload.msg.console_write = *reinterpret_cast<const rpc_pb_ConsoleWrite*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_datastore_put_tag) {
    payload.msg.datastore_put = *reinterpret_cast<const rpc_pb_DatastorePut*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_datastore_get_tag) {
    payload.msg.datastore_get = *reinterpret_cast<const rpc_pb_DatastoreGet*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_mailbox_push_tag) {
    payload.msg.mailbox_push = *reinterpret_cast<const rpc_pb_MailboxPush*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_process_run_async_tag) {
    payload.msg.process_run_async = *reinterpret_cast<const rpc_pb_ProcessRunAsync*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_process_poll_tag) {
    payload.msg.process_poll = *reinterpret_cast<const rpc_pb_ProcessPoll*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_process_kill_tag) {
    payload.msg.process_kill = *reinterpret_cast<const rpc_pb_ProcessKill*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_spi_config_tag) {
    payload.msg.spi_config = *reinterpret_cast<const rpc_pb_SpiConfig*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_spi_transfer_tag) {
    payload.msg.spi_transfer = *reinterpret_cast<const rpc_pb_SpiTransfer*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_file_write_tag) {
    payload.msg.file_write = *reinterpret_cast<const rpc_pb_FileWrite*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_file_read_tag) {
    payload.msg.file_read = *reinterpret_cast<const rpc_pb_FileRead*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_file_remove_tag) {
    payload.msg.file_remove = *reinterpret_cast<const rpc_pb_FileRemove*>(&msg);
  } else if (tag == rpc_pb_RpcPayload_get_version_tag) {
    payload.msg.get_version = rpc_pb_Empty_init_default;
  } else if (tag == rpc_pb_RpcPayload_ok_tag) {
    payload.msg.ok = rpc_pb_Empty_init_default;
  } else if (tag == rpc_pb_RpcPayload_malformed_tag) {
    payload.msg.malformed = rpc_pb_Empty_init_default;
  } else if (tag == rpc_pb_RpcPayload_ack_tag) {
    payload.msg.ack = *reinterpret_cast<const rpc_pb_AckPacket*>(&msg);
  }

  pb_ostream_t stream = pb_ostream_from_buffer(
      frame.envelope.payload.bytes, 64U);
  pb_encode(&stream, rpc_pb_RpcPayload_fields, &payload);
  frame.envelope.payload.size = static_cast<pb_size_t>(stream.bytes_written);
}

}  // namespace bridge::test

#endif
