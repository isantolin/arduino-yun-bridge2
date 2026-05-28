#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p;
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) {
    etl::copy_n(key.begin(), k_copy, p.key);
  }
  p.key[k_copy] = '\0';

  const size_t v_copy = etl::min(value.size(), sizeof(p.value.bytes));
  p.value.size = (pb_size_t)v_copy;
  if (v_copy > 0U) {
    etl::copy_n(value.data(), v_copy, p.value.bytes);
  }
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, p);
}

[[maybe_unused]] void DataStoreClass::get(etl::string_view key,
                                          GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc::payload::DatastoreGet p;
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) {
    etl::copy_n(key.begin(), k_copy, p.key);
  }
  p.key[k_copy] = '\0';

  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_GET, 0, p)) {
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
    const rpc::payload::DatastoreGetResponse& msg) {
  if (_pending_gets.empty()) return;

  const PendingGet pending = _pending_gets.front();
  _pending_gets.pop();
  if (!pending.handler.is_valid()) return;

  const etl::string_view key(pending.key.data());
  pending.handler(key,
                  etl::span<const uint8_t>(msg.value.bytes, msg.value.size));
}

DataStoreClass DataStore;

#endif
