#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#undef min
#undef max
#include <etl/delegate.h>
#include <etl/span.h>
#include <etl/string.h>
#include <etl/string_view.h>

#include "Bridge.h"
#include "protocol/PendingRpc.h"
#include "protocol/rpc_structs.h"

class DataStoreClass : public bridge::BridgeObserver {
 public:
  using GetHandler =
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass();
  static void set(etl::string_view key, etl::span<const uint8_t> value);
  void get(etl::string_view key, GetHandler handler);

  void _onResponse(const rpc::payload::DatastoreGetResponse& msg);

  void onLost() override { _pending_gets.clear(); }

  bridge::PendingRpcManager<rpc::payload::DatastoreGetResponse,
                            bridge::config::MAX_PENDING_DATASTORE, GetHandler>
      _pending_gets;
};

using DataStoreType = DataStoreClass;
extern DataStoreType DataStore;

#endif
