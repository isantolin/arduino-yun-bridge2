#include "services/Mailbox.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass() {}

void MailboxClass::_onIncomingData(const rpc_pb_MailboxPush& msg) {
  (void)msg;
}

void MailboxClass::_onIncomingData(const rpc_pb_MailboxReadResponse& msg) {
  (void)msg;
}

void MailboxClass::_onAvailableResponse(const rpc_pb_MailboxAvailableResponse& msg) {
  (void)msg;
}

void MailboxClass::push(etl::span<const uint8_t> data) {
  rpc_pb_MailboxPush p = rpc_pb_MailboxPush_init_default;
  rpc::payload::copy_to_pb_bytes(p.data, data.data(), data.size());
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_PUSH, 0, rpc_pb_MailboxPush_fields, p);
}

void MailboxClass::requestRead() {
  rpc_pb_MailboxReadResponse p = rpc_pb_MailboxReadResponse_init_default;
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_READ, 0, rpc_pb_MailboxReadResponse_fields, p);
}

void MailboxClass::requestAvailable() {
  rpc_pb_MailboxAvailableResponse p = rpc_pb_MailboxAvailableResponse_init_default;
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_AVAILABLE, 0, rpc_pb_MailboxAvailableResponse_fields, p);
}

void MailboxClass::signalProcessed() {
  rpc_pb_MailboxProcessed p = rpc_pb_MailboxProcessed_init_default;
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_PROCESSED, 0, rpc_pb_MailboxProcessed_fields, p);
}

MailboxClass Mailbox;

#endif
