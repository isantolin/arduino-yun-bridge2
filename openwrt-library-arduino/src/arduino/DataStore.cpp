#include "Bridge.h"
#include "arduino/StringUtils.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

// [OPTIMIZATION] PROGMEM error string defined in Bridge.cpp
extern const char kDatastoreQueueFull[] PROGMEM;

DataStoreClass::DataStoreClass() 
  : _datastore_get_handler(nullptr) {
  _pending_keys.clear();
}

void DataStoreClass::put(const char* key, const char* value) {
  if (!key || !value) return;

  const auto key_info = measure_bounded_cstring(
      key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH);
  if (key_info.length == 0 || key_info.overflowed) {
    return;
  }

  const auto value_info = measure_bounded_cstring(
      value, rpc::RPC_MAX_DATASTORE_KEY_LENGTH);
  if (value_info.overflowed) {
    return;
  }

  const size_t key_len = key_info.length;
  const size_t value_len = value_info.length;

  const size_t payload_len = 2 + key_len + value_len;
  if (payload_len > rpc::MAX_PAYLOAD_SIZE) return;

  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);
  payload[1 + key_len] = static_cast<uint8_t>(value_len);
  memcpy(payload + 2 + key_len, value, value_len);

  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_DATASTORE_PUT,
      payload, static_cast<uint16_t>(payload_len));
}

void DataStoreClass::requestGet(const char* key) {
  if (!key) return;
  const auto key_info = measure_bounded_cstring(
      key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH);
  if (key_info.length == 0 || key_info.overflowed) return;
  const size_t key_len = key_info.length;

  uint8_t* payload = Bridge.getScratchBuffer();
  
  payload[0] = static_cast<uint8_t>(key_len);
  memcpy(payload + 1, key, key_len);

  if (!_trackPendingDatastoreKey(key)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, reinterpret_cast<const __FlashStringHelper*>(kDatastoreQueueFull));
    return;
  }

  (void)Bridge.sendFrame(
      rpc::CommandId::CMD_DATASTORE_GET,
      payload, static_cast<uint16_t>(key_len + 1));
}

void DataStoreClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  if (command == rpc::CommandId::CMD_DATASTORE_GET_RESP) {
      const size_t payload_length = frame.header.payload_length;
      const uint8_t* payload_data = frame.payload;
      
      if (payload_length >= 1 && payload_data != nullptr) {
        uint8_t value_len = payload_data[0];
        const size_t expected = static_cast<size_t>(1 + value_len);
        if (payload_length >= expected) {
          const uint8_t* value_ptr = payload_data + 1;
          const char* key = _popPendingDatastoreKey();
          if (_datastore_get_handler) {
            _datastore_get_handler(key, value_ptr, value_len);
          }
        }
      }
  }
}

void DataStoreClass::onDataStoreGetResponse(DataStoreGetHandler handler) {
  _datastore_get_handler = handler;
}

bool DataStoreClass::_trackPendingDatastoreKey(const char* key) {
  if (!key || !*key) {
    return false;
  }

  const auto info = measure_bounded_cstring(key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH);
  if (info.length == 0 || info.overflowed) {
    return false;
  }

  if (_pending_keys.full()) {
    return false;
  }

  PendingKey pk;
  strncpy(pk.key, key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH);
  pk.key[rpc::RPC_MAX_DATASTORE_KEY_LENGTH] = '\0';
  
  _pending_keys.push(pk);
  return true;
}

const char* DataStoreClass::_popPendingDatastoreKey() {
  static char key_buffer[rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 1] = {0};
  if (_pending_keys.empty()) {
    key_buffer[0] = '\0';
    return key_buffer;
  }

  PendingKey pk = _pending_keys.front();
  _pending_keys.pop();
  
  strncpy(key_buffer, pk.key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH);
  key_buffer[rpc::RPC_MAX_DATASTORE_KEY_LENGTH] = '\0';
  
  return key_buffer;
}