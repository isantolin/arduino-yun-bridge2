#include "Bridge.h"
#include "services/Mailbox.h"
#include "protocol/PacketBuilder.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass() {}

void MailboxClass::send(etl::string_view message) {
  if (message.empty()) return;
  send(reinterpret_cast<const uint8_t*>(message.data()), message.length());
}

void MailboxClass::send(const uint8_t* data, size_t length) {
  if (!data || length == 0) return;

  etl::array<uint8_t, 2> header;
  rpc::write_u16_be(header.data(), static_cast<uint16_t>(length));

  Bridge.sendChunkyFrame(rpc::CommandId::CMD_MAILBOX_PUSH, 
                         header.data(), header.size(), 
                         data, length);
}

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

#endif
