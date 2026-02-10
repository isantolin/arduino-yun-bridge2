#include "Bridge.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

DataStoreClass::DataStoreClass() 
  : _datastore_get_handler(nullptr) {
  _last_datastore_key.clear();
}

void DataStoreClass::put(const char* key, const char* value) {
  if (!key || *key == '\0' || !value) return;
  if (!Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_PUT, 
                                key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH,
                                value, rpc::RPC_MAX_DATASTORE_KEY_LENGTH)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("DataStore too large"));
  }
}

void DataStoreClass::requestGet(const char* key) {
  if (!key || *key == '\0') return;
  if (!_trackPendingDatastoreKey(key)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("Key too long"));
    return;
  }

  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_DATASTORE_GET, 
                                key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH)) {
    _popPendingDatastoreKey(); // Clean up if send failed
    Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, F("Key too long"));
  }
}

void DataStoreClass::handleResponse(const rpc::Frame& frame) {
  const rpc::CommandId command = static_cast<rpc::CommandId>(frame.header.command_id);
  if (command == rpc::CommandId::CMD_DATASTORE_GET_RESP) {
      const size_t payload_length = frame.header.payload_length;
      const uint8_t* payload_data = frame.payload.data();
      
      if (payload_length >= 1) {
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

const char* DataStoreClass::_popPendingDatastoreKey() {
  if (_pending_datastore_keys.empty()) {
    _last_datastore_key.clear();
    return _last_datastore_key.c_str();
  }

  _last_datastore_key = _pending_datastore_keys.front();
  _pending_datastore_keys.pop();
  return _last_datastore_key.c_str();
}

bool DataStoreClass::_trackPendingDatastoreKey(const char* key) {
  if (!key) return false;
  etl::string_view sv(key);
  if (sv.empty() || sv.length() > rpc::RPC_MAX_DATASTORE_KEY_LENGTH) {
    return false;
  }

  if (_pending_datastore_keys.full()) {
    return false;
  }

  _pending_datastore_keys.push(etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>(sv.data(), sv.length()));
  return true;
}
