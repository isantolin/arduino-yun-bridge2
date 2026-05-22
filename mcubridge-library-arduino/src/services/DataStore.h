#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/array.h>
#include <etl/delegate.h>
#include <etl/queue.h>
#include <etl/span.h>
#include <etl/string_view.h>

#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

class DataStoreClass : public BridgeObserver {
 public:
  using GetHandler =
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass();
  static void set(etl::string_view key, etl::span<const uint8_t> value);

  void _onResponse(const rpc::payload::DatastoreGetResponse& msg);

  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { _pending_gets.clear(); }

  struct PendingGet {
    etl::array<char, rpc::RPC_MAX_DATASTORE_KEY_LENGTH + 1U> key;
    GetHandler handler;
  };
  etl::queue<PendingGet, bridge::config::MAX_PENDING_DATASTORE> _pending_gets;
};

extern DataStoreClass DataStore;

#endif
