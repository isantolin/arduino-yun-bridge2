#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_DATASTORE
#include "etl/delegate.h"
#include "etl/queue.h"
#include "etl/span.h"
#include "etl/string.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"
#include "protocol/BridgeEvents.h"
#include "router/command_router.h"
#include "etl/message_router.h"

class DataStoreClass : public BridgeObserver, public etl::imessage_router {
 public:
  using DataStoreGetHandler = etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass();
  void begin() { reset(); }
  void reset();

  // [SIL-2] imessage_router interface
  void receive(const etl::imessage& msg) override;
  bool accepts(etl::message_id_t id) const override;
  bool is_null_router() const override { return false; }
  bool is_producer() const override { return true; }
  bool is_consumer() const override { return true; }

  // [SIL-2] Observer Interface
  void notification(MsgBridgeLost) override { reset(); }

  void put(etl::string_view key, etl::string_view value);
  void requestGet(etl::string_view key);
  inline void onDataStoreGetResponse(DataStoreGetHandler handler) { _datastore_get_handler = handler; }

  bool _trackPendingDatastoreKey(etl::string_view key);
  etl::string_view _popPendingDatastoreKey();

  DataStoreGetHandler _datastore_get_handler;
  etl::queue<etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH>, BRIDGE_MAX_PENDING_DATASTORE> _pending_datastore_keys;
  etl::string<rpc::RPC_MAX_DATASTORE_KEY_LENGTH> _last_datastore_key;
};

extern DataStoreClass DataStore;
#endif
#endif
