#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

template <typename T>
DataStoreClass<T>::DataStoreClass() {}

template <typename T>
void DataStoreClass<T>::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p;
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) {
    etl::copy_n(key.begin(), k_copy, p.key);
  }

  const size_t v_copy = etl::min(value.size(), sizeof(p.value.bytes));
  p.value.size = (pb_size_t)v_copy;
  if (v_copy > 0U) {
    etl::copy_n(value.data(), v_copy, p.value.bytes);
  }
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, p);
}

template <typename T>
void DataStoreClass<T>::get(etl::string_view key,
                            typename DataStoreClass<T>::GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc::payload::DatastoreGet p;
  const size_t k_copy = etl::min(key.size(), sizeof(p.key) - 1U);
  if (k_copy > 0U) {
    etl::copy_n(key.begin(), k_copy, p.key);
  }

  if (!Bridge.send(rpc::CommandId::CMD_DATASTORE_GET, 0, p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  typename DataStoreClass<T>::PendingGet pending = {};
  const size_t to_copy = etl::min(key.length(), pending.key.size() - 1U);
  etl::copy_n(key.data(), to_copy, pending.key.begin());
  pending.handler = handler;
  _pending_gets.push(pending);
}

template <typename T>
void DataStoreClass<T>::_onResponse(
    const rpc::payload::DatastoreGetResponse& msg) {
  if (_pending_gets.empty()) return;

  const typename DataStoreClass<T>::PendingGet pending = _pending_gets.front();
  _pending_gets.pop();
  if (!pending.handler.is_valid()) return;

  const etl::string_view key(pending.key.data());
  pending.handler(key,
                  etl::span<const uint8_t>(msg.value.bytes, msg.value.size));
}

template class DataStoreClass<void>;
DataStoreType DataStore;

#endif
