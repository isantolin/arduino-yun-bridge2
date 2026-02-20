#include "Bridge.h"
#include <string.h>
#include "protocol/rpc_protocol.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {
  reset();
}

void DataStoreClass::reset() {
  _last_datastore_key.clear();
  while (!_pending_datastore_keys.empty()) {
    _pending_datastore_keys.pop();
  }
}

void DataStoreClass::put(etl::string_view key, etl::string_view value) {
  if (key.empty() || value.empty()) return;
  if (!Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_PUT, 
                                key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH,
                                value, rpc::RPC_MAX_DATASTORE_KEY_LENGTH)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
  }
}

void DataStoreClass::requestGet(etl::string_view key) {
  if (key.empty()) return;
  if (!_trackPendingDatastoreKey(key)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_DATASTORE_GET, 
                                key, rpc::RPC_MAX_DATASTORE_KEY_LENGTH)) {
    _popPendingDatastoreKey(); // Clean up if send failed
    Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
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

bool DataStoreClass::_trackPendingDatastoreKey(etl::string_view key) {
  if (key.empty() || key.length() > rpc::RPC_MAX_DATASTORE_KEY_LENGTH) {
    return false;
  }

  if (_pending_datastore_keys.full()) {
    return false;
  }

  _pending_datastore_keys.push(etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>(key.data(), key.length()));
  return true;
}

#endif
