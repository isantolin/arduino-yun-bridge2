#include "services/Mailbox.h"

#include "Bridge.h"
#include "protocol/PacketBuilder.h"

#if BRIDGE_ENABLE_MAILBOX

MailboxClass::MailboxClass()
    : etl::imessage_router(etl::imessage_router::MESSAGE_ROUTER) {}

void MailboxClass::receive(const etl::imessage& msg) {
  const uint16_t cmd = static_cast<uint16_t>(msg.get_message_id());
  if (cmd != rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH) &&
      cmd != rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP) &&
      cmd != rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP)) {
    return;
  }

  const auto& cmd_msg = static_cast<const bridge::router::CommandMessage&>(msg);

  if (cmd == rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH)) {
    Bridge._withPayloadAck<rpc::payload::MailboxPush>(
        cmd_msg, [this](const rpc::payload::MailboxPush& pl) {
          _onIncomingData(etl::span<const uint8_t>(pl.data, pl.length));
        });
  } else if (cmd == rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP)) {
    Bridge._withPayload<rpc::payload::MailboxReadResponse>(
        cmd_msg, [this](const rpc::payload::MailboxReadResponse& pl) {
          _onIncomingData(etl::span<const uint8_t>(pl.content, pl.length));
        });
  } else if (cmd ==
             rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP)) {
    Bridge._withPayload<rpc::payload::MailboxAvailableResponse>(
        cmd_msg, [this](const rpc::payload::MailboxAvailableResponse& pl) {
          if (_mailbox_available_handler.is_valid()) {
            _mailbox_available_handler(pl.count);
          }
        });
  }
}

bool MailboxClass::accepts(etl::message_id_t id) const {
  const uint16_t cmd = static_cast<uint16_t>(id);
  return cmd == rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH) ||
         cmd == rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP) ||
         cmd == rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
}

void MailboxClass::send(etl::string_view message) {
  if (message.empty()) return;
  send(etl::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(message.data()), message.length()));
}

void MailboxClass::send(etl::span<const uint8_t> data) {
  if (data.empty()) return;

  etl::vector<uint8_t, 2> header;
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
  if (_mailbox_handler.is_valid()) {
    _mailbox_handler(data);
  }
}

#endif
#if BRIDGE_ENABLE_MAILBOX
MailboxClass Mailbox;
#endif
