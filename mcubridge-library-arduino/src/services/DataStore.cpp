#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include "services/DataStore.h"

#include <pw_span/span.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

void DataStoreClass::set(etl::string_view key, pw::span<const uint8_t> value) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(pw::Status::Unavailable());
    return;
  }
  rpc_pb_DatastorePut p = rpc_pb_DatastorePut_init_default;
  bridge::pb::copy_string(p.key, sizeof(p.key), key);
  bridge::pb::copy_bytes(p.value, value);

  (void)Bridge.dataStoreClient().Put(p);
}

void DataStoreClass::get(etl::string_view key,
                         typename DataStoreClass::GetHandler handler) {
  if (_get_call.active() || !Bridge.isRpcTxReady()) {
    Bridge.emitStatus(_get_call.active() ? pw::Status::ResourceExhausted()
                                         : pw::Status::Unavailable());
    return;
  }

  rpc_pb_DatastoreGet p = rpc_pb_DatastoreGet_init_default;
  bridge::pb::copy_string(p.key, sizeof(p.key), key);

  _pending_get_handler = handler;
  _get_call = Bridge.dataStoreClient().Get(
      p,
      [](const rpc_pb_DatastoreGetResponse& resp, pw::Status status) {
        DataStore._onResponse(resp, status);
      },
      [](pw::Status status) { DataStore._onError(status); });
}

void DataStoreClass::_onResponse(const rpc_pb_DatastoreGetResponse& msg,
                                 pw::Status status) {
  if (!status.ok()) {
    _onError(status);
    return;
  }
  if (_pending_get_handler.is_valid()) {
    GetHandler handler = _pending_get_handler;
    _pending_get_handler = {};
    handler(etl::string_view(),
            pw::span<const uint8_t>(msg.value.bytes, msg.value.size));
  }
}

void DataStoreClass::_onError(pw::Status status) {
  _pending_get_handler = {};
  Bridge.emitStatus(status);
}

DataStoreType DataStore;

#endif
