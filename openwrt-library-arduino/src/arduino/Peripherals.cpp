#include "Bridge.h"

#include <string.h>

#include "protocol/rpc_protocol.h"

using namespace rpc;

DataStoreClass::DataStoreClass() {}

void DataStoreClass::put(const char* key, const char* value) {
  if (!key || !value) return;

  size_t key_len = strlen(key);
  size_t value_len = strlen(value);
  if (key_len == 0 || key_len > BridgeClass::kMaxDatastoreKeyLength ||
      value_len > BridgeClass::kMaxDatastoreKeyLength) {
    return;
  }

  const size_t payload_len = 2 + key_len + value_len;
  if (payload_len > MAX_PAYLOAD_SIZE) return;

  uint8_t payload[MAX_PAYLOAD_SIZE];
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);
  payload[1 + key_len] = static_cast<uint8_t>(value_len);
  memcpy(payload + 2 + key_len, value, value_len);

  Bridge.sendFrame(CMD_DATASTORE_PUT, payload,
                   static_cast<uint16_t>(payload_len));
}

void DataStoreClass::requestGet(const char* key) {
  if (!key) return;
  size_t key_len = strlen(key);
  if (key_len == 0 || key_len > BridgeClass::kMaxDatastoreKeyLength) return;

  uint8_t payload[1 + 255];
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);

  Bridge._trackPendingDatastoreKey(key);
  // Linux responde con CMD_DATASTORE_GET_RESP usando únicamente su caché.
  Bridge.sendFrame(CMD_DATASTORE_GET, payload,
                   static_cast<uint16_t>(key_len + 1));
}

MailboxClass::MailboxClass() {}

void MailboxClass::send(const char* message) {
  if (!message) return;
  send(reinterpret_cast<const uint8_t*>(message), strlen(message));
}

void MailboxClass::send(const uint8_t* data, size_t length) {
  if (!data || length == 0) return;

  size_t max_payload = MAX_PAYLOAD_SIZE - 2;
  if (length > max_payload) {
    length = max_payload;
  }

  uint8_t payload[MAX_PAYLOAD_SIZE];
  write_u16_be(payload, static_cast<uint16_t>(length));
  memcpy(payload + 2, data, length);
  Bridge.sendFrame(CMD_MAILBOX_PUSH, payload,
                   static_cast<uint16_t>(length + 2));
}

void MailboxClass::requestRead() {
  Bridge.sendFrame(CMD_MAILBOX_READ, nullptr, 0);
}

void MailboxClass::requestAvailable() {
  Bridge.sendFrame(CMD_MAILBOX_AVAILABLE, nullptr, 0);
}

void FileSystemClass::write(const char* filePath, const uint8_t* data,
                            size_t length) {
  if (!filePath || !data) return;
  size_t path_len = strlen(filePath);
  if (path_len == 0 || path_len > 255) return;

  const size_t max_data = MAX_PAYLOAD_SIZE - 3 - path_len;
  if (length > max_data) {
    length = max_data;
  }

  uint8_t payload[MAX_PAYLOAD_SIZE];
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  write_u16_be(payload + 1 + path_len, static_cast<uint16_t>(length));
  if (length > 0) {
    memcpy(payload + 3 + path_len, data, length);
  }

  Bridge.sendFrame(CMD_FILE_WRITE, payload,
                   static_cast<uint16_t>(path_len + length + 3));
}

void FileSystemClass::remove(const char* filePath) {
  if (!filePath) return;
  size_t path_len = strlen(filePath);
  if (path_len == 0 || path_len > 255) return;

  uint8_t payload[1 + 255];
  payload[0] = static_cast<uint8_t>(path_len);
  memcpy(payload + 1, filePath, path_len);
  Bridge.sendFrame(CMD_FILE_REMOVE, payload,
                   static_cast<uint16_t>(path_len + 1));
}

ProcessClass::ProcessClass() {}

void ProcessClass::kill(int pid) {
  uint8_t pid_payload[2];
  write_u16_be(pid_payload, static_cast<uint16_t>(pid));
  Bridge.sendFrame(CMD_PROCESS_KILL, pid_payload, 2);
}
