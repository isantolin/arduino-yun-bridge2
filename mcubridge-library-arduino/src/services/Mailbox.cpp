#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

namespace {

void send_mailbox_command(rpc::CommandId command_id) {
  if (!Bridge.sendFrame(command_id)) {
  }
}

}  // namespace

template <typename T>
MailboxClass<T>::MailboxClass() {}

template <typename T>
void MailboxClass<T>::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p = {};
  const size_t to_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)to_copy;
  if (to_copy > 0U) {
    etl::copy_n(data.data(), to_copy, p.data.bytes);
  }
  if (!Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p)) {
  }
}

template <typename T>
typename MailboxClass<T>::MessageCallback MailboxClass<T>::_message_callback;

template <typename T>
typename MailboxClass<T>::AvailableCallback
    MailboxClass<T>::_available_callback;

template <typename T>
etl::queue<typename MailboxClass<T>::MailboxMessage, 8> MailboxClass<T>::_queue;

template <typename T>
void MailboxClass<T>::requestRead() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_READ);
}

template <typename T>
void MailboxClass<T>::requestAvailable() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

template <typename T>
void MailboxClass<T>::signalProcessed(uint32_t message_id) {
  rpc::payload::MailboxProcessed p = {};
  p.message_id = message_id;
  if (!Bridge.send(rpc::CommandId::CMD_MAILBOX_PROCESSED, 0, p)) {
  }
}

template <typename T>
void MailboxClass<T>::_onPush(const rpc::payload::MailboxPush& msg) {
  if (!_queue.full()) {
    MailboxMessage m;
    m.size = (uint8_t)etl::min((size_t)msg.data.size, sizeof(m.data));
    etl::copy_n(msg.data.bytes, m.size, m.data.begin());
    _queue.push(m);
  }
}

template <typename T>
void MailboxClass<T>::_onReadResponse(
    const rpc::payload::MailboxReadResponse& msg) {
  if (!_queue.full()) {
    MailboxMessage m;
    m.size = (uint8_t)etl::min((size_t)msg.content.size, sizeof(m.data));
    etl::copy_n(msg.content.bytes, m.size, m.data.begin());
    _queue.push(m);
  }
}

template <typename T>
void MailboxClass<T>::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  if (_available_callback) {
    _available_callback(msg.count);
  }
}

template <typename T>
void MailboxClass<T>::process() {
  if (!_queue.empty() && _message_callback) {
    const auto& m = _queue.front();
    _message_callback(etl::span<const uint8_t>(m.data.data(), m.size));
    _queue.pop();
  }
}

template <typename T>
void MailboxClass<T>::onLost() {
  _queue.clear();
}

template class MailboxClass<void>;
MailboxType Mailbox;

#endif
