#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

namespace {

void send_mailbox_command(rpc::CommandId command_id) {
  [[maybe_unused]] auto _u1 = Bridge.sendFrame(command_id);
}

}  // namespace

template <typename T>
MailboxClass<T>::MailboxClass() {}

template <typename T>
void MailboxClass<T>::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p;
  const size_t to_copy = etl::min(data.size(), sizeof(p.data.bytes));
  p.data.size = (pb_size_t)to_copy;
  if (to_copy > 0U) {
    etl::copy_n(data.data(), to_copy, p.data.bytes);
  }
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p);
}

template <typename T>
typename MailboxClass<T>::MessageCallback MailboxClass<T>::_message_callback;

template <typename T>
typename MailboxClass<T>::AvailableCallback MailboxClass<T>::_available_callback;

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
  rpc::payload::MailboxProcessed p;
  p.message_id = message_id;
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_MAILBOX_PROCESSED, 0, p);
}

template <typename T>
void MailboxClass<T>::_onPush(const rpc::payload::MailboxPush& msg) {
  if (_message_callback) {
    _message_callback(etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
  }
}

template <typename T>
void MailboxClass<T>::_onReadResponse(const rpc::payload::MailboxReadResponse& msg) {
  if (_message_callback) {
    _message_callback(etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

template <typename T>
void MailboxClass<T>::_onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg) {
  if (_available_callback) {
    _available_callback(msg.count);
  }
}

template class MailboxClass<void>;
MailboxType Mailbox;

#endif
