#include "DataStore.h"
#include "Bridge.h"
#include "util/pb_copy.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut msg = {};
  rpc::util::pb_copy_string(key, msg.key, sizeof(msg.key));
  rpc::util::pb_setup_encode_span(msg.value, value);
  Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_PUT, 0, msg);
}

void DataStoreClass::get(etl::string_view key, DataStoreGetHandler handler) {
  if (_pending_gets.full()) return;
  rpc::payload::DatastoreGet msg = {};
  rpc::util::pb_copy_string(key, msg.key, sizeof(msg.key));
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_GET, 0, msg)) {
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
