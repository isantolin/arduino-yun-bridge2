#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

namespace {

void send_mailbox_command(rpc::CommandId command_id) {
  (void)Bridge.sendFrame(command_id);
}

}  // namespace

MailboxClass::MailboxClass() : _rx_buffer(), _available_count(0U) {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p;
  rpc::payload::copy_to_pb_bytes(p.data, data.data(), data.size());
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p);
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

void MailboxClass::_setIncomingData(etl::span<const uint8_t> data) {
  _rx_buffer.clear();
  etl::for_each(data.begin(), data.end(), [this](uint8_t b) {
    if (!_rx_buffer.full()) _rx_buffer.push(b);
  });
}

void MailboxClass::_onIncomingData(const rpc::payload::MailboxPush& msg) {
  _setIncomingData(
      etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

void MailboxClass::_onIncomingData(
    const rpc::payload::MailboxReadResponse& msg) {
  _setIncomingData(etl::span<const uint8_t>(msg.content.bytes,
                                            msg.content.size));
}

void MailboxClass::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  _available_count = msg.count;
}

MailboxClass Mailbox;

#endif
