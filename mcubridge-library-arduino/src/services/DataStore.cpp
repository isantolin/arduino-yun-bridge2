#include "Bridge.h"
#include "protocol/rpc_protocol.h"

// [OPTIMIZATION] Numerical status codes used instead of PROGMEM strings.

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass()
    : etl::imessage_router(
          rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)) {
  reset();
}

void DataStoreClass::reset() {
  _pending_datastore_keys.clear();
  _last_datastore_key.clear();
}

void DataStoreClass::receive(const etl::imessage& msg) {
  if (msg.get_message_id() != rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP)) return;
  const auto& cmd_msg = static_cast<const bridge::router::CommandMessage&>(msg);
  Bridge._withPayload<rpc::payload::DatastoreGetResponse>(
      cmd_msg, [this](const rpc::payload::DatastoreGetResponse& pl) {
        if (_datastore_get_handler.is_valid()) {
          _datastore_get_handler(_popPendingDatastoreKey(), etl::span<const uint8_t>(pl.value, pl.value_len));
        }
      });
}

bool DataStoreClass::accepts(etl::message_id_t id) const {
  return id == rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
}

void DataStoreClass::put(etl::string_view key, etl::string_view value) {
  if (key.empty()) return;
  (void)Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_PUT, key,
                                rpc::RPC_MAX_DATASTORE_KEY_LENGTH, value,
                                64); // Use literal if constant missing
}

void DataStoreClass::requestGet(etl::string_view key) {
  if (key.empty()) return;

  if (_trackPendingDatastoreKey(key)) {
    (void)Bridge.sendStringCommand(rpc::CommandId::CMD_DATASTORE_GET, key,
                                  rpc::RPC_MAX_DATASTORE_KEY_LENGTH);
  }
}

bool DataStoreClass::_trackPendingDatastoreKey(etl::string_view key) {
  if (_pending_datastore_keys.full()) {
    return false;
  }

  _pending_datastore_keys.push(
      etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>(key.data(), key.length()));
  return true;
}

etl::string_view DataStoreClass::_popPendingDatastoreKey() {
  if (_pending_datastore_keys.empty()) return "";
  _last_datastore_key = _pending_datastore_keys.front();
  _pending_datastore_keys.pop();
  return _last_datastore_key;
}

#endif
#if BRIDGE_ENABLE_DATASTORE
DataStoreClass DataStore;
#endif
