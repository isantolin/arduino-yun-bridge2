#include "Mailbox.h"
#include "Bridge.h"
#include "util/pb_copy.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass() {}

void MailboxClass::write(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush msg = {};
  rpc::util::pb_setup_encode_span(msg.data, data);
  Bridge.sendPbCommand(rpc::CommandId::CMD_MAILBOX_PUSH, msg);
}

void MailboxClass::requestRead() {
  Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::_onIncomingData(etl::span<const uint8_t> data) {
  BRIDGE_ATOMIC_BLOCK {
    const size_t space = _rx_buffer.capacity() - _rx_buffer.size();
    const size_t to_copy = etl::min(data.size(), space);
    _rx_buffer.push(data.begin(), data.begin() + to_copy);
  }
  if (_mailbox_handler.is_valid()) {
    _mailbox_handler(data);
  }
}

void MailboxClass::_onResponse(etl::span<const uint8_t> content) {
  _onIncomingData(content);
}

void MailboxClass::_onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg) {
  if (_available_handler.is_valid()) {
    _available_handler(static_cast<uint16_t>(msg.count));
  }
}
#endif
