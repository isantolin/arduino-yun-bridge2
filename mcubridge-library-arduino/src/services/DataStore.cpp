#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc_pb_DatastorePut p = rpc_pb_DatastorePut_init_default;
  rpc::payload::copy_to_pb_string(p.key, key);
  rpc::payload::copy_to_pb_bytes(p.value, value.data(), value.size());
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, rpc_pb_DatastorePut_fields, p);
}

void DataStoreClass::get(etl::string_view key, GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc_pb_DatastoreGet p = rpc_pb_DatastoreGet_init_default;
  rpc::payload::copy_to_pb_string(p.key, key);
  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_GET, 0, rpc_pb_DatastoreGet_fields, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  PendingGet pending = {};
  const size_t to_copy = etl::min(key.length(), pending.key.size() - 1U);
  etl::copy_n(key.data(), to_copy, pending.key.begin());
  pending.key[to_copy] = '\0';
  pending.handler = handler;
  _pending_gets.push(pending);
}

void DataStoreClass::_onResponse(
    const rpc_pb_DatastoreGetResponse& msg) {
  if (_pending_gets.empty()) return;

  const PendingGet pending = _pending_gets.front();
  _pending_gets.pop();
  if (!pending.handler.is_valid()) return;

  const etl::string_view key(pending.key.data());
  pending.handler(key, etl::span<const uint8_t>(msg.value.bytes,
                                                msg.value.size));
}

DataStoreClass DataStore;

#endif
