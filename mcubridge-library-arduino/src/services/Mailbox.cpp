#include "services/Mailbox.h"
#include "Bridge.h"
#if BRIDGE_ENABLE_MAILBOX
MailboxClass::MailboxClass() : _rx_buffer(), _available_count(0U) {}
void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc_pb_MailboxPush p = rpc_pb_MailboxPush_init_default;
  p.data.funcs.encode = &BridgeClass::_encode_span_callback;
  p.data.arg = (void*)&data;
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, p);
}
void MailboxClass::requestRead() { [[maybe_unused]] auto _u1 = Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ); }
void MailboxClass::requestAvailable() { [[maybe_unused]] auto _u1 = Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE); }
void MailboxClass::signalProcessed() { [[maybe_unused]] auto _u1 = Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_PROCESSED); }
void MailboxClass::_onAvailableResponse(const rpc::payload::MailboxAvailableResponse& msg) { _available_count = msg.count; }
void MailboxClass::_setIncomingData(etl::span<const uint8_t> data) {
  _rx_buffer.clear();
  for (auto b : data) { if (!_rx_buffer.full()) _rx_buffer.push(b); }
}
MailboxClass Mailbox;
#endif
