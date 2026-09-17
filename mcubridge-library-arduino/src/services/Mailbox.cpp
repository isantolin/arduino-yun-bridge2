#include "services/Mailbox.h"

#include <etl/algorithm.h>

#if BRIDGE_ENABLE_MAILBOX

#include "Bridge.h"

MailboxClass::MailboxClass() {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p = {};
  rpc::Payload::copy_to_pb_bytes(p.data, data);
  (void)Bridge.sendOrEmitStatus(
      rpc::CommandId::CMD_MAILBOX_PUSH, 0, p,
      etl::string_view(rpc::status_reason::MAILBOX_PUSH_FAILED));
}

typename MailboxClass::MessageCallback MailboxClass::_message_callback;

typename MailboxClass::AvailableCallback MailboxClass::_available_callback;

etl::queue<typename MailboxClass::MailboxBuffer, 8> MailboxClass::_queue;

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::signalProcessed(uint32_t message_id) {
  rpc::payload::MailboxProcessed p = {};
  p.message_id = message_id;
  (void)Bridge.sendOrEmitStatus(
      rpc::CommandId::CMD_MAILBOX_PROCESSED, 0, p,
      etl::string_view(rpc::status_reason::MAILBOX_PROCESSED_FAILED));
}

void MailboxClass::_enqueue(etl::span<const uint8_t> data) {
  if (!_queue.full()) {
    MailboxBuffer m;
    const size_t sz = etl::min(data.size(), m.capacity());
    m.assign(data.data(), data.data() + sz);
    _queue.push(m);
  }
}

void MailboxClass::_onPush(const rpc::payload::MailboxPush& msg) {
  _enqueue(etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

void MailboxClass::_onReadResponse(
    const rpc::payload::MailboxReadResponse& msg) {
  _enqueue(etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
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
    _message_callback(etl::span<const uint8_t>(m.data(), m.size()));
    _queue.pop();
  }
}

void MailboxClass::onLost() { _queue.clear(); }

MailboxType Mailbox;

#endif
