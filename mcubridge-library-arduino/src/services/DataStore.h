#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/delegate.h>
#include <etl/queue.h>
#include <etl/span.h>
#include <etl/string.h>
#include <etl/string_view.h>

#include "protocol/rpc_structs.h"

class DataStoreClass {
 public:
  using GetHandler =
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass();
  static void set(etl::string_view key, etl::span<const uint8_t> value);
  void get(etl::string_view key, GetHandler handler);

  void _onResponse(const rpc::payload::DatastoreGetResponse& msg);

  void onLost() { _pending_gets.clear(); }

  etl::queue<GetHandler, bridge::config::MAX_PENDING_DATASTORE> _pending_gets;
};

using DataStoreType = DataStoreClass;
extern DataStoreType DataStore;

#endif
