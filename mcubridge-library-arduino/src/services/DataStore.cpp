#include "services/DataStore.h"
#include "Bridge.h"
#include <etl/algorithm.h>

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  rpc::payload::DatastorePut msg = {};
  msg.key = key;
  msg.value = value;
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0, msg);
}

void DataStoreClass::get(etl::string_view key, etl::delegate<void(etl::string_view, etl::span<const uint8_t>)> handler) {
  (void)handler;
  rpc::payload::DatastoreGet msg = {};
  msg.key = key;
  if (Bridge.send(rpc::CommandId::CMD_DATASTORE_GET, 0, msg)) {
    PendingGet pg;
    const size_t len = etl::min(key.size(), sizeof(pg.key) - 1);
    etl::copy_n(key.data(), len, pg.key);
    pg.key[len] = '\0';
    _pending_gets.push(pg);
  }
}

void DataStoreClass::_onResponse(const rpc::payload::DatastoreGetResponse& msg) {
  (void)msg;
}

#ifndef BRIDGE_TEST_NO_GLOBALS
DataStoreClass DataStore;
#endif

#endif
