#include "DataStore.h"
#include "Bridge.h"
#include "util/string_copy.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut msg = {};
  rpc::util::copy_string(key, msg.key, sizeof(msg.key));
  msg.value = value;
  Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_PUT, 0, msg);
}

[[maybe_unused]] void DataStoreClass::get(etl::string_view key, DataStoreGetHandler handler) {
  if (_pending_gets.full()) return;
  rpc::payload::DatastoreGet msg = {};
  rpc::util::copy_string(key, msg.key, sizeof(msg.key));
  if (Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_GET, 0, msg)) {
    _pending_gets.push({handler, key});
  }
}

void DataStoreClass::_onResponse(etl::span<const uint8_t> value) {
  if (_pending_gets.empty()) return;
  const auto& pending = _pending_gets.front();
  if (pending.handler.is_valid()) {
    pending.handler(pending.key, value);
  }
  _pending_gets.pop();
}
#endif
