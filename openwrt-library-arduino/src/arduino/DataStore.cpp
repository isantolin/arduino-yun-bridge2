#include "Bridge.h"
#include "arduino/StringUtils.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

using namespace rpc;

DataStoreClass::DataStoreClass() 
  : _pending_datastore_head(0),
    _pending_datastore_count(0),
    _datastore_get_handler(nullptr) {
  for (auto& key : _pending_datastore_keys) {
    memset(key.data(), 0, key.size());
  }
  memset(_pending_datastore_key_lengths.data(), 0, _pending_datastore_key_lengths.size());
}

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

  if (!_trackPendingDatastoreKey(key)) {
    Bridge._emitStatus(StatusCode::STATUS_ERROR, "datastore_queue_full");
    return;
  }

  (void)Bridge.sendFrame(
      CommandId::CMD_DATASTORE_GET,
      payload, static_cast<uint16_t>(key_len + 1));
}

void DataStoreClass::handleResponse(const rpc::Frame& frame) {
  const CommandId command = static_cast<CommandId>(frame.header.command_id);
  if (command == CommandId::CMD_DATASTORE_GET_RESP) {
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

  const auto info = measure_bounded_cstring(key, BridgeClass::kMaxDatastoreKeyLength);
  if (info.length == 0 || info.overflowed) {
    return false;
  }
  const size_t length = info.length;

  if (_pending_datastore_count >= kMaxPendingDatastore) {
    return false;
  }

  uint8_t slot =
      (_pending_datastore_head + _pending_datastore_count) %
      kMaxPendingDatastore;
  memcpy(_pending_datastore_keys[slot].data(), key, length);
  _pending_datastore_keys[slot][length] = '\0';
  _pending_datastore_key_lengths[slot] = static_cast<uint8_t>(length);
  _pending_datastore_count++;
  return true;
}

const char* DataStoreClass::_popPendingDatastoreKey() {
  static char key_buffer[BridgeClass::kMaxDatastoreKeyLength + 1] = {0};
  if (_pending_datastore_count == 0) {
    key_buffer[0] = '\0';
    return key_buffer;
  }

  uint8_t slot = _pending_datastore_head;
  uint8_t length = _pending_datastore_key_lengths[slot];
  if (length > BridgeClass::kMaxDatastoreKeyLength) {
    length = BridgeClass::kMaxDatastoreKeyLength;
  }
  memcpy(key_buffer, _pending_datastore_keys[slot].data(), length);
  key_buffer[length] = '\0';
  _pending_datastore_head =
      (_pending_datastore_head + 1) % kMaxPendingDatastore;
  _pending_datastore_count--;
  _pending_datastore_key_lengths[slot] = 0;
  _pending_datastore_keys[slot][0] = '\0';
  return key_buffer;
}
