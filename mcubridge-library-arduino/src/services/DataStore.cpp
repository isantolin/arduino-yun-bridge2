#include "DataStore.h"
#include "Bridge.h"
#include "util/pb_copy.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  Bridge.sendKeyDataCommand(rpc::CommandId::CMD_DATASTORE_PUT, key, &rpc::payload::DatastorePut::key, value, &rpc::payload::DatastorePut::value);
}

void DataStoreClass::get(etl::string_view key, DataStoreGetHandler handler) {
  if (_pending_gets.full()) return;
  if (Bridge.sendKeyCommand(rpc::CommandId::CMD_DATASTORE_GET, key, &rpc::payload::DatastoreGet::key)) {
    _pending_gets.push({handler, key});
  }
}

void DataStoreClass::_onResponse(etl::span<const uint8_t> value) {
  if (_pending_gets.empty()) return;
  PendingGet pending = _pending_gets.front();
  _pending_gets.pop();
  if (pending.handler.is_valid()) {
    pending.handler(pending.key, value);
  }
}
#endif
