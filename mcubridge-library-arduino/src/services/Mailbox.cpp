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
  rpc::PacketBuilder(header).add_value(static_cast<uint16_t>(data.size()));

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

  BRIDGE_ATOMIC_BLOCK {
    const size_t space = _rx_buffer.capacity() - _rx_buffer.size();
    const size_t to_copy = etl::min(data.size(), space);
    _rx_buffer.push(data.begin(), data.begin() + to_copy);
  }

  if (_mailbox_handler.is_valid()) {
    _mailbox_handler(data);
  }
}

#endif
