#include "services/Mailbox.h"

#include <etl/algorithm.h>

#if BRIDGE_ENABLE_MAILBOX

#include "Bridge.h"

MailboxClass::MailboxClass() {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p = {};
  const size_t to_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)to_copy;
  if (to_copy > 0U) {
    etl::copy_n(data.data(), to_copy, p.data.bytes);
  }
  if (!Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p)) {
  }
}

typename MailboxClass::MessageCallback MailboxClass::_message_callback;

typename MailboxClass::AvailableCallback MailboxClass::_available_callback;

etl::queue<typename MailboxClass::MailboxMessage, 8> MailboxClass::_queue;

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::signalProcessed(uint32_t message_id) {
  rpc::payload::MailboxProcessed p = {};
  p.message_id = message_id;
  if (!Bridge.send(rpc::CommandId::CMD_MAILBOX_PROCESSED, 0, p)) {
  }
}

void MailboxClass::_onPush(const rpc::payload::MailboxPush& msg) {
  if (!_queue.full()) {
    MailboxMessage m;
    m.size = (uint8_t)etl::min((size_t)msg.data.size, sizeof(m.data));
    etl::copy_n(msg.data.bytes, m.size, m.data.begin());
    _queue.push(m);
  }
}

void MailboxClass::_onReadResponse(
    const rpc::payload::MailboxReadResponse& msg) {
  if (!_queue.full()) {
    MailboxMessage m;
    m.size = (uint8_t)etl::min((size_t)msg.content.size, sizeof(m.data));
    etl::copy_n(msg.content.bytes, m.size, m.data.begin());
    _queue.push(m);
  }
}

void MailboxClass::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  if (_available_callback) {
    _available_callback(msg.count);
  }
}

void MailboxClass::process() {
  if (!_queue.empty() && _message_callback) {
    const auto& m = _queue.front();
    _message_callback(etl::span<const uint8_t>(m.data.data(), m.size));
    _queue.pop();
  }
}

void MailboxClass::onLost() { _queue.clear(); }

MailboxType Mailbox;

#endif
