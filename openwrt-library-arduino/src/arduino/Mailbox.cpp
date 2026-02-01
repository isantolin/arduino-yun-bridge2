#include "Bridge.h"
#include "arduino/StringUtils.h"
#include "etl/algorithm.h"
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

  // [SIL-2] Large Message Support
  // We remove the explicit 2-byte length prefix that was present in the old implementation
  // because the Frame Header already contains the payload length.
  // This allows us to use standard chunking for messages > 64 bytes.
  // Note: The receiving side (Python) will receive these as separate messages.
  // Reassembly is up to the application layer if needed.
  Bridge.sendChunkyFrame(rpc::CommandId::CMD_MAILBOX_PUSH, 
                         nullptr, 0, 
                         data, length);
}

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(rpc::CommandId::CMD_MAILBOX_AVAILABLE);
}

void MailboxClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  const uint8_t* payload_data = frame.payload.data();
  const size_t payload_length = frame.header.payload_length;

      if (command == rpc::CommandId::CMD_MAILBOX_READ_RESP) {
          if (_mailbox_handler && payload_length >= 2) {
            uint16_t msg_len = rpc::read_u16_be(payload_data);
            const uint8_t* msg_ptr = payload_data + 2;
            if (payload_length >= static_cast<size_t>(2 + msg_len)) {
              _mailbox_handler(msg_ptr, msg_len);
            }
          }
      } else if (command == rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP) {      if (_mailbox_available_handler && payload_length >= 2) {
        uint16_t count = rpc::read_u16_be(payload_data);
        _mailbox_available_handler(count);
      }
  }
}
