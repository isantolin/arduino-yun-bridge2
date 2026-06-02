#ifndef RPC_SERVICES_H
#define RPC_SERVICES_H

#include <etl/string_view.h>
#include <etl/span.h>
#include <etl/delegate.h>
#include "rpc_protocol.h"
#include "rpc_structs.h"

// Forward declaration
class BridgeClass;
extern BridgeClass Bridge;

namespace rpc::services {

namespace datastore {
    using GetHandler = BridgeClass::DataStoreGetHandler;
    
    inline void put(etl::string_view key, etl::span<const uint8_t> value);
    inline void get(etl::string_view key, GetHandler handler);
}

} // namespace rpc::services

#include "Bridge.h"

namespace rpc::services {
namespace datastore {
    inline void put(etl::string_view key, etl::span<const uint8_t> value) {
        Bridge.datastorePut(key, value);
    }
    inline void get(etl::string_view key, GetHandler handler) {
        Bridge.datastoreGet(key, handler);
    }
}
}

#endif

namespace rpc::services {
namespace filesystem {
    using ReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;
    inline void write(etl::string_view path, etl::span<const uint8_t> data) { Bridge.fileWrite(path, data); }
    inline void read(etl::string_view path, ReadHandler handler) { Bridge.fileRead(path, handler); }
    inline void remove(etl::string_view path) { Bridge.fileRemove(path); }
}
}
