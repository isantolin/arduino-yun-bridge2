#include "Bridge.h"
#include "arduino/StringUtils.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

using namespace rpc;

MailboxClass::MailboxClass() {}

void MailboxClass::send(const char* message) {
  if (!message) return;
  const size_t max_payload = MAX_PAYLOAD_SIZE - 2;
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

  size_t max_payload = MAX_PAYLOAD_SIZE - 2;
  if (length > max_payload) {
    length = max_payload;
  }

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
  write_u16_be(payload, static_cast<uint16_t>(length));
  memcpy(payload + 2, data, length);
  (void)Bridge.sendFrame(
      CommandId::CMD_MAILBOX_PUSH,
      payload, static_cast<uint16_t>(length + 2));
}

void MailboxClass::requestRead() {
  (void)Bridge.sendFrame(CommandId::CMD_MAILBOX_READ);
}

void MailboxClass::requestAvailable() {
  (void)Bridge.sendFrame(CommandId::CMD_MAILBOX_AVAILABLE);
}
