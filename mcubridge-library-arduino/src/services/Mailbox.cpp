#include "Mailbox.h"
#include "Bridge.h"
#include "util/string_copy.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass() {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush msg = {};
  msg.data = data;
  Bridge.sendPbCommand(rpc::CommandId::CMD_MAILBOX_PUSH, 0, msg);
}

[[maybe_unused]] void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

[[maybe_unused]] void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::_onIncomingData(etl::span<const uint8_t> data) {
  BridgeClass::safePush(_rx_buffer, data);
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
