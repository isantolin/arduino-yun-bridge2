#include "services/Mailbox.h"

#include "Bridge.h"
#include "protocol/PacketBuilder.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass() = default;

void MailboxClass::send(etl::string_view message) {
  if (message.empty()) return;
  send(etl::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(message.data()), message.length()));
}

void MailboxClass::send(etl::span<const uint8_t> data) {
  if (data.empty()) return;

  constexpr size_t MAILBOX_HEADER_SIZE = 2;
  etl::vector<uint8_t, MAILBOX_HEADER_SIZE> header;
  rpc::PacketBuilder(header).add_all(static_cast<uint16_t>(data.size()));

  Bridge.sendChunkyFrame(rpc::CommandId::CMD_MAILBOX_PUSH,
                         etl::span<const uint8_t>(header.data(), header.size()),
                         data);
}

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::_onIncomingData(etl::span<const uint8_t> data) {
  if (data.empty()) return;

  // [SIL-2] Use centralized safe push with atomic protection
  Bridge.safePush(_rx_buffer, data);

  if (_mailbox_handler.is_valid()) {
    _mailbox_handler(data);
  }
}

#endif
