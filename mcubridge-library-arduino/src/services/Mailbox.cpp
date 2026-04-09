#include "services/Mailbox.h"
#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass() {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush msg = {};
  msg.data = data;
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, msg);
}

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::signalProcessed() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_PROCESSED);
}

void MailboxClass::_onIncomingData(const rpc::payload::MailboxPush& msg) {
  _rx_buffer.clear();
  etl::for_each(msg.data.begin(), msg.data.end(), [this](uint8_t b) {
    if (!_rx_buffer.full()) _rx_buffer.push(b);
  });
}

void MailboxClass::_onIncomingData(const rpc::payload::MailboxReadResponse& msg) {
  _rx_buffer.clear();
  etl::for_each(msg.content.begin(), msg.content.end(), [this](uint8_t b) {
    if (!_rx_buffer.full()) _rx_buffer.push(b);
  });
}

void MailboxClass::_onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg) {
  (void)msg; // Handled by accessor in tests or higher level logic
}

#ifndef BRIDGE_TEST_NO_GLOBALS
MailboxClass Mailbox;
#endif

#endif
