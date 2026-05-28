#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

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
  const size_t to_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)to_copy;
  if (to_copy > 0U) {
    etl::copy_n(data.data(), to_copy, p.data.bytes);
  }
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
  _setIncomingData(etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

void MailboxClass::_onIncomingData(
    const rpc::payload::MailboxReadResponse& msg) {
  _setIncomingData(
      etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
}

void MailboxClass::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  _available_count = msg.count;
}

MailboxClass Mailbox;

#endif
