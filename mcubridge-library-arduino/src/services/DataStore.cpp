#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include "services/DataStore.h"

#include <pw_span/span.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

void DataStoreClass::set(etl::string_view key, pw::span<const uint8_t> value) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }
  rpc_pb_DatastorePut p = rpc_pb_DatastorePut_init_default;
  bridge::pb::copy_string(p.key, sizeof(p.key), key);
  bridge::pb::copy_bytes(p.value, value);

  (void)Bridge.dataStoreClient().Put(p);
}

void DataStoreClass::get(etl::string_view key,
                         typename DataStoreClass::GetHandler handler) {
  if (_pending_gets.full() || !Bridge.isRpcTxReady()) {
    Bridge.emitStatus(_pending_gets.full()
                          ? rpc::StatusCode::STATUS_RESOURCE_EXHAUSTED
                          : rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }

  rpc_pb_DatastoreGet p = rpc_pb_DatastoreGet_init_default;
  bridge::pb::copy_string(p.key, sizeof(p.key), key);

  _get_call = Bridge.dataStoreClient().Get(
      p, [](const rpc_pb_DatastoreGetResponse& resp, pw::Status status) {
        static_cast<void>(status);
        DataStore._onResponse(resp);
      });

  _pending_gets.push(handler);
}

void DataStoreClass::_onResponse(const rpc_pb_DatastoreGetResponse& msg) {
  if (!_pending_gets.empty()) {
    GetHandler handler = _pending_gets.front();
    _pending_gets.pop();
    if (handler.is_valid()) {
      handler(etl::string_view(),
              pw::span<const uint8_t>(msg.value.bytes, msg.value.size));
    }
  }
}

DataStoreType DataStore;

#endif
