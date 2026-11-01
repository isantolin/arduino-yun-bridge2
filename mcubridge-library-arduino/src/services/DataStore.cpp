#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc_pb_DatastorePut p = rpc_pb_DatastorePut_init_default;
  bridge::pb::copy_string(p.key, sizeof(p.key), key);
  bridge::pb::copy_bytes(p.value, value);

  constexpr uint32_t kServiceId =
      pw::tokenizer::Hash("bridge.DataStoreService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Put");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_DatastorePut_fields,
                             p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void DataStoreClass::get(etl::string_view key,
                         typename DataStoreClass::GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc_pb_DatastoreGet p = rpc_pb_DatastoreGet_init_default;
  bridge::pb::copy_string(p.key, sizeof(p.key), key);

  constexpr uint32_t kServiceId =
      pw::tokenizer::Hash("bridge.DataStoreService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Get");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_DatastoreGet_fields,
                             p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  _pending_gets.push(handler);
}

void DataStoreClass::_onResponse(const rpc_pb_DatastoreGetResponse& msg) {
  if (!_pending_gets.empty()) {
    GetHandler handler = _pending_gets.front();
    _pending_gets.pop();
    if (handler.is_valid()) {
      handler(etl::string_view(),
              etl::span<const uint8_t>(msg.value.bytes, msg.value.size));
    }
  }
}

DataStoreType DataStore;

#endif
