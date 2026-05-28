#include "services/DataStore.h"

#include "Bridge.h"
#include "protocol/pb_field_helpers.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p;
  rpc::pb_field::copy_string_view_trunc(key, p.key);
  rpc::pb_field::copy_span_to_bytes_field(value, p.value);
  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, p)) {
    Bridge.enterSafeState();
  }
}

void DataStoreClass::get(etl::string_view key, GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc::payload::DatastoreGet p;
  rpc::pb_field::copy_string_view_trunc(key, p.key);

  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_GET, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  PendingGet pending = {};
  rpc::pb_field::copy_string_view_trunc(key, pending.key);
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
  pending.handler(key, rpc::pb_field::bytes_field_as_span(msg.value));
}

DataStoreClass DataStore;

#endif
