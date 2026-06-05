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
void MailboxClass<T>::requestRead() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_READ);
}

template <typename T>
void MailboxClass<T>::requestAvailable() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

template <typename T>
void MailboxClass<T>::signalProcessed() {
  send_mailbox_command(rpc::CommandId::CMD_MAILBOX_PROCESSED);
}

template <typename T>
void MailboxClass<T>::_onIncomingData(const rpc::payload::MailboxPush& msg) {
  (void)msg;
}

template <typename T>
void MailboxClass<T>::_onIncomingData(
    const rpc::payload::MailboxReadResponse& msg) {
  (void)msg;
}

template <typename T>
void MailboxClass<T>::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  (void)msg;
}

template class MailboxClass<void>;
MailboxType Mailbox;

#endif
