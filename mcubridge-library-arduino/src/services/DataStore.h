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
  void set(etl::string_view key, etl::span<const uint8_t> value);
  void get(etl::string_view key, etl::delegate<void(etl::string_view, etl::span<const uint8_t>)> handler);

  void _onResponse(const rpc::payload::DatastoreGetResponse& msg);

  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { _pending_gets.clear(); }

  struct PendingGet { char key[16]; };
  etl::queue<PendingGet, bridge::config::MAX_PENDING_DATASTORE> _pending_gets;
};

extern DataStoreClass DataStore;

#endif
