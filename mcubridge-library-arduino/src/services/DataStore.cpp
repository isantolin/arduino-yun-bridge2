#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p = {};
  p.key[key.copy(p.key, sizeof(p.key) - 1)] = '\0';
  const size_t bounded_size = etl::min(value.size(), sizeof(p.value.bytes));
  if (bounded_size > 0U) {
    etl::copy_n(value.data(), bounded_size, p.value.bytes);
  }
  p.value.size = static_cast<pb_size_t>(bounded_size);

  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, p)) {
    Bridge.emitStatus(
        rpc::StatusCode::STATUS_ERROR,
        etl::string_view(rpc::status_reason::DATASTORE_PUT_FAILED));
  }
}

void DataStoreClass::get(etl::string_view key,
                         typename DataStoreClass::GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc::payload::DatastoreGet p = {};
  p.key[key.copy(p.key, sizeof(p.key) - 1)] = '\0';

  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_GET, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  _pending_gets.push(handler);
}

void DataStoreClass::_onResponse(
    const rpc::payload::DatastoreGetResponse& msg) {
  if (_pending_gets.empty()) return;

  const GetHandler handler = _pending_gets.front();
  _pending_gets.pop();
  if (!handler.is_valid()) return;

  handler(etl::string_view(),
          etl::span<const uint8_t>(msg.value.bytes, msg.value.size));
}

DataStoreType DataStore;

#endif
