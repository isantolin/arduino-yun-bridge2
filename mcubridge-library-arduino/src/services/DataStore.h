#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#include <stdint.h>
#include "config/bridge_config.h"
#undef min
#undef max
#include "etl/delegate.h"
#include "etl/queue.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge { namespace test { class DataStoreTestAccessor; } }
#endif

class DataStoreClass : public BridgeObserver {
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::DataStoreTestAccessor;
#endif
 public:
  using DataStoreGetHandler = etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass();

  // [SIL-2] Observer Interface
  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { _pending_gets.clear(); }
  void notification(MsgBridgeError) override {}
  void notification(MsgBridgeCommand) override {}

  void set(etl::string_view key, etl::span<const uint8_t> value);
  void put(etl::string_view key, etl::span<const uint8_t> value) { set(key, value); }
  void put(etl::string_view key, etl::string_view value) {
    set(key, etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>(value.data()), value.size()));
  }
  void get(etl::string_view key, DataStoreGetHandler handler);
  void reset() { _pending_gets.clear(); }

  void _onResponse(etl::span<const uint8_t> value);

 private:
  struct PendingGet {
    DataStoreGetHandler handler;
    etl::string_view key;
  };

  // [SIL-2] Use ETL containers for safe queue management
  etl::queue<PendingGet, bridge::config::MAX_PENDING_DATASTORE> _pending_gets;
};

#if BRIDGE_ENABLE_DATASTORE
extern DataStoreClass DataStore;
#endif

#endif
