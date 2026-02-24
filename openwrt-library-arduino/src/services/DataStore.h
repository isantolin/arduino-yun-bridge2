#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_DATASTORE
#include "etl/string.h"
#include "etl/string_view.h"
#include "etl/span.h"
#include "etl/delegate.h"
#include "etl/flat_map.h"
#include "etl/queue.h"
#include "protocol/rpc_protocol.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge {
namespace test {
  class DataStoreTestAccessor;
}
}
#endif

class BridgeClass;

class DataStoreClass {
  friend class BridgeClass;
  #if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::DataStoreTestAccessor;
  #endif
 public:
  using DataStoreGetHandler = etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass();
  void reset();
  void put(etl::string_view key, etl::string_view value);
  void requestGet(etl::string_view key);
  inline void onDataStoreGetResponse(DataStoreGetHandler handler) {
    _datastore_get_handler = handler;
  }

 private:
  bool _trackPendingDatastoreKey(etl::string_view key);
  const char* _popPendingDatastoreKey();

  DataStoreGetHandler _datastore_get_handler;

  // [OPTIMIZATION] Use flat_map for O(log n) key lookup
  etl::flat_map<etl::string<16>, etl::string<16>, 8> _local_cache;
  etl::queue<etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>, BRIDGE_MAX_PENDING_DATASTORE> _pending_datastore_keys;
  etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH> _last_datastore_key;
};

extern DataStoreClass DataStore;
#endif

#endif
