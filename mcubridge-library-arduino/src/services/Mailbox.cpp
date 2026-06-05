#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

namespace {

void send_mailbox_command(rpc::CommandId command_id) {
  [[maybe_unused]] auto _u1 = Bridge.sendFrame(command_id);
}

}  // namespace

MailboxClass::MailboxClass() {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p;
  const size_t to_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)to_copy;
  if (to_copy > 0U) {
    etl::copy_n(data.data(), to_copy, p.data.bytes);
  }
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p);
}

[[maybe_unused]] void MailboxClass::requestRead() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_READ);
}

[[maybe_unused]] void MailboxClass::requestAvailable() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

[[maybe_unused]] void MailboxClass::signalProcessed() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_PROCESSED);
}

void MailboxClass::_onIncomingData(const rpc::payload::MailboxPush& msg) {
  (void)msg;
}

void MailboxClass::_onIncomingData(
    const rpc::payload::MailboxReadResponse& msg) {
  (void)msg;
}

void MailboxClass::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  (void)msg;
}

MailboxClass Mailbox;

#endif
