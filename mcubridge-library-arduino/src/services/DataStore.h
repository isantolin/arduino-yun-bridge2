#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/string_view.h>
#include <etl/span.h>
#include <etl/queue.h>
#include <etl/delegate.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

class DataStoreClass : public BridgeObserver {
 public:
  DataStoreClass();
  [[maybe_unused]] static void set(etl::string_view key, etl::span<const uint8_t> value);

  static void _onResponse(const rpc::payload::DatastoreGetResponse& msg);

  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { }
};

extern DataStoreClass DataStore;

#endif
