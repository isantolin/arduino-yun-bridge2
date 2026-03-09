#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_DATASTORE
#include "etl/delegate.h"
#include "etl/optional.h"
#include "etl/queue.h"
#include "etl/span.h"
#include "etl/string.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge {
namespace test {
class DataStoreTestAccessor;
}
}  // namespace bridge
#endif

#include "protocol/BridgeEvents.h"

class BridgeClass;

class DataStoreClass : public BridgeObserver {
  friend class BridgeClass;
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::DataStoreTestAccessor;
#endif
 public:
  using DataStoreGetHandler =
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass();
  void reset();

  // [SIL-2] Observer Interface
  void notification(MsgBridgeLost) override { reset(); }

  void put(etl::string_view key, etl::string_view value);
  void requestGet(etl::string_view key);
  inline void onDataStoreGetResponse(DataStoreGetHandler handler) {
    _datastore_get_handler = handler;
  }

 private:
  bool _trackPendingDatastoreKey(etl::string_view key);
  etl::optional<etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>> _popPendingDatastoreKey();

  DataStoreGetHandler _datastore_get_handler;

  etl::queue<etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>,
             BRIDGE_MAX_PENDING_DATASTORE>
      _pending_datastore_keys;
};

extern DataStoreClass DataStore;
#endif

#endif
