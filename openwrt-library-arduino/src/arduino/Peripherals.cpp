#include "Bridge.h"

#include "arduino/StringUtils.h"

#include <string.h>

#include "protocol/rpc_protocol.h"

using namespace rpc;

DataStoreClass::DataStoreClass() {}

void DataStoreClass::put(const char* key, const char* value) {
  if (!key || !value) return;

  const auto key_info = measure_bounded_cstring(
      key, BridgeClass::kMaxDatastoreKeyLength);
  if (key_info.length == 0 || key_info.overflowed) {
    return;
  }

  const auto value_info = measure_bounded_cstring(
      value, BridgeClass::kMaxDatastoreKeyLength);
  if (value_info.overflowed) {
    return;
  }

  const size_t key_len = key_info.length;
  const size_t value_len = value_info.length;

  const size_t payload_len = 2 + key_len + value_len;
  if (payload_len > MAX_PAYLOAD_SIZE) return;

  // [OPTIMIZATION] Use shared scratch buffer instead of stack allocation
  // Original: uint8_t payload[MAX_PAYLOAD_SIZE];
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);
  payload[1 + key_len] = static_cast<uint8_t>(value_len);
  memcpy(payload + 2 + key_len, value, value_len);

  (void)Bridge.sendFrame(
      CommandId::CMD_DATASTORE_PUT,
      payload, static_cast<uint16_t>(payload_len));
}

void DataStoreClass::requestGet(const char* key) {
  if (!key) return;
  const auto key_info = measure_bounded_cstring(
      key, BridgeClass::kMaxDatastoreKeyLength);
  if (key_info.length == 0 || key_info.overflowed) return;
  const size_t key_len = key_info.length;

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);

  if (!Bridge._trackPendingDatastoreKey(key)) {
    Bridge._emitStatus(StatusCode::STATUS_ERROR, "datastore_queue_full");
    return;
  }

  (void)Bridge.sendFrame(
      CommandId::CMD_DATASTORE_GET,
      payload, static_cast<uint16_t>(key_len + 1));
}

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

void FileSystemClass::write(const char* filePath, const uint8_t* data,
                            size_t length) {
  if (!filePath || !data) return;
  const auto path_info = measure_bounded_cstring(
      filePath, BridgeClass::kMaxFilePathLength);
  if (path_info.length == 0 || path_info.overflowed) return;
  const size_t path_len = path_info.length;

  const size_t max_data = MAX_PAYLOAD_SIZE - 3 - path_len;
  if (length > max_data) {
    length = max_data;
  }

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  write_u16_be(payload + 1 + path_len, static_cast<uint16_t>(length));
  if (length > 0) {
    memcpy(payload + 3 + path_len, data, length);
  }

  (void)Bridge.sendFrame(
      CommandId::CMD_FILE_WRITE,
      payload, static_cast<uint16_t>(path_len + length + 3));
}

void FileSystemClass::remove(const char* filePath) {
  if (!filePath) return;
  const auto path_info = measure_bounded_cstring(
      filePath, BridgeClass::kMaxFilePathLength);
  if (path_info.length == 0 || path_info.overflowed) return;
  const size_t path_len = path_info.length;

  // [OPTIMIZATION] Use shared scratch buffer
  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  (void)Bridge.sendFrame(
      CommandId::CMD_FILE_REMOVE,
      payload, static_cast<uint16_t>(path_len + 1));
}

ProcessClass::ProcessClass() {}

void ProcessClass::kill(int pid) {
  uint8_t pid_payload[2];
  write_u16_be(pid_payload, static_cast<uint16_t>(pid));
  (void)Bridge.sendFrame(CommandId::CMD_PROCESS_KILL, pid_payload, 2);
}
