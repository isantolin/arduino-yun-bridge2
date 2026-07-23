#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p = {};
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) {
    etl::copy_n(key.begin(), k_copy, p.key);
  }

  const size_t v_copy = etl::min(value.size(), sizeof(p.value.bytes));
  p.value.size = (pb_size_t)v_copy;
  if (v_copy > 0U) {
    etl::copy_n(value.data(), v_copy, p.value.bytes);
  }
  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, p)) {
  }
}

void DataStoreClass::get(etl::string_view key,
                         typename DataStoreClass::GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc::payload::DatastoreGet p = {};
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) {
    etl::copy_n(key.begin(), k_copy, p.key);
  }

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
