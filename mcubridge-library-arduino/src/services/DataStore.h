#ifndef SERVICES_DATASTORE_H
#define SERVICES_DATASTORE_H

#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/span.h>
#include <etl/string_view.h>

#include "Bridge.h"
#include "protocol/mcu_wire.pb.h"

class DataStoreClass : public bridge::BridgeObserver {
 public:
  using GetHandler =
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;

  DataStoreClass() = default;
  static void set(etl::string_view key, etl::span<const uint8_t> value);
  void get(etl::string_view key, GetHandler handler);

  void _onResponse(const rpc_pb_DatastoreGetResponse& msg);

  void onLost() override {
    _get_call = {};
    _pending_gets.clear();
  }

 private:
  pw::rpc::NanopbUnaryReceiver<rpc_pb_DatastoreGetResponse> _get_call{};
  etl::circular_buffer<GetHandler, bridge::config::MAX_PENDING_DATASTORE>
      _pending_gets;
};

using DataStoreType = DataStoreClass;
extern DataStoreType DataStore;

#endif
