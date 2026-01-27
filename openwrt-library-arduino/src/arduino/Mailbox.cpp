#include "Bridge.h"
#include "arduino/StringUtils.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

MailboxClass::MailboxClass() 
  : _mailbox_handler(nullptr),
    _mailbox_available_handler(nullptr) {}

void MailboxClass::send(const char* message) {
  if (!message) return;
  const size_t max_payload = rpc::MAX_PAYLOAD_SIZE - 2;
  const auto info = measure_bounded_cstring(message, max_payload);
  if (info.length == 0) {
    return;
  }
  size_t length = info.length;
  if (info.overflowed) {
    length = max_payload;
  }
  send(reinterpret_cast<const uint8_t*>(message), length);
}

void MailboxClass::send(const uint8_t* data, size_t length) {
  if (!data || length == 0) return;

  size_t max_payload = rpc::MAX_PAYLOAD_SIZE - 2;
  if (length > max_payload) {
    length = max_payload;
  }

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
    rpc::write_u16_be(payload, static_cast<uint16_t>(length));
  memcpy(payload + 2, data, length);
  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_MAILBOX_PUSH,
      payload, static_cast<uint16_t>(length + 2));
}

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const size_t payload_length = frame.header.payload_length;
  const uint8_t* payload_data = frame.payload.data();

  switch (command) {
    case rpc::CommandId::CMD_MAILBOX_READ_RESP:
      if (_mailbox_handler && payload_length >= 2 && payload_data != nullptr) {
        uint16_t message_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + message_len);
        if (payload_length >= expected) {
          const uint8_t* body_ptr = payload_data + 2;
          _mailbox_handler(body_ptr, message_len);
        }
      }
      break;
    case rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP:
      if (_mailbox_available_handler && payload_length == 1 && payload_data) {
        uint8_t count = payload_data[0];
        _mailbox_available_handler(count);
      }
      break;
    case rpc::CommandId::CMD_MAILBOX_PUSH:
      if (_mailbox_handler && payload_length >= 2 && payload_data != nullptr) {
        uint16_t message_len = rpc::read_u16_be(payload_data);
        const size_t expected = static_cast<size_t>(2 + message_len);
        if (payload_length >= expected) {
          const uint8_t* body_ptr = payload_data + 2;
          _mailbox_handler(body_ptr, message_len);
        }
      }
      break;
    default:
      break;
  }
}

void MailboxClass::onMailboxMessage(MailboxHandler handler) {
  _mailbox_handler = handler;
}

void MailboxClass::onMailboxAvailableResponse(MailboxAvailableHandler handler) {
  _mailbox_available_handler = handler;
}
