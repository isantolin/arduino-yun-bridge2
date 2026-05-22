#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

namespace {

#if 0
void send_mailbox_command(rpc::CommandId command_id) {
  (void)Bridge.sendFrame(command_id);
}
#endif

}  // namespace

MailboxClass::MailboxClass() : _rx_buffer(), _available_count(0U) {}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc::payload::MailboxPush p;
  rpc::payload::copy_to_pb_bytes(p.pb_msg.data, data.data(), data.size());
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p);
}

void MailboxClass::_setIncomingData(etl::span<const uint8_t> data) {
  _rx_buffer.clear();
  etl::for_each(data.begin(), data.end(), [this](uint8_t b) {
    if (!_rx_buffer.full()) _rx_buffer.push(b);
  });
}

void MailboxClass::_onIncomingData(const rpc::payload::MailboxPush& msg) {
  _setIncomingData(
      etl::span<const uint8_t>(msg.pb_msg.data.bytes, msg.pb_msg.data.size));
}

void MailboxClass::_onIncomingData(
    const rpc::payload::MailboxReadResponse& msg) {
  _setIncomingData(etl::span<const uint8_t>(msg.pb_msg.content.bytes,
                                            msg.pb_msg.content.size));
}

void MailboxClass::_onAvailableResponse(
    const rpc::payload::MailboxAvailableResponse& msg) {
  _available_count = msg.pb_msg.count;
}

MailboxClass Mailbox;

#endif
