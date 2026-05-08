#include "services/DataStore.h"
#include "Bridge.h"
#include <etl/algorithm.h>

#if BRIDGE_ENABLE_DATASTORE

DataStoreClass::DataStoreClass() {}

void DataStoreClass::set(etl::string_view key, etl::span<const uint8_t> value) {
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 0,
                    rpc::payload::DatastorePut{key, value});
}

void DataStoreClass::_onResponse(const rpc::payload::DatastoreGetResponse& msg) {
  (void)msg;
}

DataStoreClass DataStore;

#endif
