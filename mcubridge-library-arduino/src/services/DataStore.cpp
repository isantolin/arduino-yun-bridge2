#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0,
                    rpc::payload::DatastorePut{key, value});
}

void DataStoreClass::get(etl::string_view key, GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_GET, 0,
                   rpc::payload::DatastoreGet{key})) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  PendingGet pending = {};
  const size_t to_copy = etl::min(key.length(), pending.key.size() - 1U);
  etl::copy_n(key.data(), to_copy, pending.key.begin());
  pending.key[to_copy] = rpc::RPC_NULL_TERMINATOR;
  pending.handler = handler;
  _pending_gets.push(pending);
}

void DataStoreClass::_onResponse(
    const rpc::payload::DatastoreGetResponse& msg) {
  if (_pending_gets.empty()) return;

  const PendingGet pending = _pending_gets.front();
  _pending_gets.pop();
  if (!pending.handler.is_valid()) return;

  const etl::string_view key(pending.key.data());
  pending.handler(key, msg.value);
}

DataStoreClass DataStore;

#endif
