#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

namespace {

void send_mailbox_command(rpc::CommandId command_id) {
  (void)Bridge.sendFrame(command_id);
}

}  // namespace

MailboxClass::MailboxClass()
    : _rx_buffer(), _available_count(0U), _available_handler() {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0,
                    rpc::payload::MailboxPush{data});
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
  _rx_buffer.clear();
  etl::for_each(data.begin(), data.end(), [this](uint8_t b) {
    if (!_rx_buffer.full()) _rx_buffer.push(b);
  });
}

void MailboxClass::_onIncomingData(const rpc::payload::MailboxPush& msg) {
  _setIncomingData(msg.data);
}

void MailboxClass::_onIncomingData(
    const rpc::payload::MailboxReadResponse& msg) {
  _setIncomingData(msg.content);
}

void MailboxClass::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  _available_count = msg.count;
  if (_available_handler.is_valid()) _available_handler(_available_count);
}

MailboxClass Mailbox;

#endif
