#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p;
  rpc::payload::copy_to_pb_string(p.pb_msg.key, key);
  rpc::payload::copy_to_pb_bytes(p.pb_msg.value, value.data(), value.size());
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, p);
}

void DataStoreClass::_onResponse(
    const rpc::payload::DatastoreGetResponse& msg) {
  if (_pending_gets.empty()) return;

  const PendingGet pending = _pending_gets.front();
  _pending_gets.pop();
  if (!pending.handler.is_valid()) return;

  const etl::string_view key(pending.key.data());
  pending.handler(key, etl::span<const uint8_t>(msg.pb_msg.value.bytes,
                                                msg.pb_msg.value.size));
}

DataStoreClass DataStore;

#endif
