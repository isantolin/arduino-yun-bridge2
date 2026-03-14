#include "services/Mailbox.h"

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass() = default;

void MailboxClass::send(etl::string_view message) {
  if (message.empty()) return;
  send(etl::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(message.data()), message.length()));
}

void MailboxClass::send(etl::span<const uint8_t> data) {
  if (data.empty()) return;

  uint8_t buffer[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t stream = pb_ostream_from_buffer(buffer, sizeof(buffer));
  
  rpc::payload::MailboxPush msg = {};
  msg.data.size = static_cast<pb_size_t>(etl::min<size_t>(data.size(), sizeof(msg.data.bytes)));
  etl::copy_n(data.begin(), msg.data.size, msg.data.bytes);

  if (pb_encode(&stream, rpc::Payload::Descriptor<rpc::payload::MailboxPush>::fields(), &msg)) {
    static_cast<void>(Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_PUSH,
                         etl::span<const uint8_t>(buffer, stream.bytes_written)));
  }
}

void MailboxClass::requestRead() {
  static_cast<void>(Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ));
}

void MailboxClass::requestAvailable() {
  static_cast<void>(Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE));
}

void MailboxClass::_onIncomingData(etl::span<const uint8_t> data) {
  if (data.empty()) return;

  // [SIL-2] Use centralized safe push with atomic protection
  Bridge.safePush(_rx_buffer, data);

  if (_mailbox_handler.is_valid()) {
    _mailbox_handler(data);
  }
}

#endif
