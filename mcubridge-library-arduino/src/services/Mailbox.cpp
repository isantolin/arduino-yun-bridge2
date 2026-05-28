#include "services/Mailbox.h"

#include "Bridge.h"
#include "protocol/pb_field_helpers.h"

#if BRIDGE_ENABLE_MAILBOX

namespace {

void send_mailbox_command(rpc::CommandId command_id) {
  if (!Bridge.sendFrame(command_id)) {
    Bridge.enterSafeState();
  }
}

}  // namespace

MailboxClass::MailboxClass() : _rx_buffer(), _available_count(0U) {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p;
  rpc::pb_field::copy_span_to_bytes_field(data, p.data);
  if (!Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p)) {
    Bridge.enterSafeState();
  }
}

void MailboxClass::requestRead() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::signalProcessed() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_PROCESSED);
}

void MailboxClass::_setIncomingData(etl::span<const uint8_t> data) {
  _rx_buffer.assign(data.begin(), data.end());
}

void MailboxClass::_onIncomingData(const rpc::payload::MailboxPush& msg) {
  _setIncomingData(rpc::pb_field::bytes_field_as_span(msg.data));
}

void MailboxClass::_onIncomingData(
    const rpc::payload::MailboxReadResponse& msg) {
  _setIncomingData(rpc::pb_field::bytes_field_as_span(msg.content));
}

void MailboxClass::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  _available_count = msg.count;
}

MailboxClass Mailbox;

#endif
