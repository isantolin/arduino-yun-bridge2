#include "services/DataStore.h"

#include <etl/algorithm.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut p = {};
  rpc::Payload::copy_to_pb_string(p.key, key);
  rpc::Payload::copy_to_pb_bytes(p.value, value);

  Bridge.sendOrEmitStatus(
      rpc::CommandId::CMD_DATASTORE_PUT, 0, p,
      etl::string_view(rpc::status_reason::DATASTORE_PUT_FAILED));
}

void DataStoreClass::get(etl::string_view key,
                         typename DataStoreClass::GetHandler handler) {
  if (_pending_gets.full()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }

  rpc::payload::DatastoreGet p = {};
  rpc::Payload::copy_to_pb_string(p.key, key);

  if (!Bridge.sendOrEmitStatus(rpc::CommandId::CMD_DATASTORE_GET, 0, p)) {
    return;
  }

  _pending_gets.push(handler);
}

void DataStoreClass::_onResponse(
    const rpc::payload::DatastoreGetResponse& msg) {
  _pending_gets.dispatch(
      msg, [](const GetHandler& handler,
              const rpc::payload::DatastoreGetResponse& response) {
        if (!handler.is_valid()) return;
        handler(etl::string_view(),
                etl::span<const uint8_t>(response.value.bytes,
                                         response.value.size));
      });
}

DataStoreType DataStore;

#endif
