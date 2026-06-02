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
    using GetHandler = etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>;
    inline void put(etl::string_view key, etl::span<const uint8_t> value);
    inline void get(etl::string_view key, GetHandler handler);
}

namespace filesystem {
    using ReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;
    inline void write(etl::string_view path, etl::span<const uint8_t> data);
    inline void read(etl::string_view path, ReadHandler handler);
    inline void remove(etl::string_view path);
}

namespace mailbox {
    inline void push(etl::span<const uint8_t> data);
    inline void requestRead();
    inline void requestAvailable();
    inline void signalProcessed();
    inline uint16_t available();
    inline int read();
    inline int peek();
}

namespace process {
    using RunHandler = etl::delegate<void(int32_t)>;
    using PollHandler = etl::delegate<void(rpc::StatusCode, uint16_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>;
    inline void runAsync(etl::string_view cmd, etl::span<const etl::string_view> args, RunHandler handler);
    inline void poll(int32_t pid, PollHandler handler);
    inline void kill(int32_t pid);
}

namespace console {
    inline size_t write(uint8_t c);
    inline size_t write(const uint8_t* buffer, size_t size);
    inline int available();
    inline int read();
    inline int peek();
}

namespace spi {
    inline void begin();
    inline void end();
    inline void setConfig(const rpc::payload::SpiConfig& config);
    inline size_t transfer(etl::span<uint8_t> buffer);
}

} // namespace rpc::services

#include "Bridge.h"

namespace rpc::services {
namespace datastore {
    inline void put(etl::string_view key, etl::span<const uint8_t> value) { Bridge.datastorePut(key, value); }
    inline void get(etl::string_view key, GetHandler handler) { Bridge.datastoreGet(key, handler); }
}
namespace filesystem {
    inline void write(etl::string_view path, etl::span<const uint8_t> data) { Bridge.fileWrite(path, data); }
    inline void read(etl::string_view path, ReadHandler handler) { Bridge.fileRead(path, handler); }
    inline void remove(etl::string_view path) { Bridge.fileRemove(path); }
}
namespace mailbox {
    inline void push(etl::span<const uint8_t> data) { Bridge.mailboxPush(data); }
    inline void requestRead() { Bridge.mailboxRequestRead(); }
    inline void requestAvailable() { Bridge.mailboxRequestAvailable(); }
    inline void signalProcessed() { Bridge.mailboxSignalProcessed(); }
    inline uint16_t available() { return Bridge.mailboxAvailable(); }
    inline int read() { return Bridge.mailboxRead(); }
    inline int peek() { return Bridge.mailboxPeek(); }
}
namespace process {
    inline void runAsync(etl::string_view cmd, etl::span<const etl::string_view> args, RunHandler handler) { Bridge.processRunAsync(cmd, args, handler); }
    inline void poll(int32_t pid, PollHandler handler) { Bridge.pollProcess(pid, handler); }
    inline void kill(int32_t pid) { Bridge.processKill(pid); }
}
namespace console {
    inline size_t write(uint8_t c) { return Bridge.consoleWrite(c); }
    inline size_t write(const uint8_t* buffer, size_t size) { return Bridge.consoleWrite(buffer, size); }
    inline int available() { return Bridge.consoleAvailable(); }
    inline int read() { return Bridge.consoleRead(); }
    inline int peek() { return Bridge.consolePeek(); }
}
namespace spi {
    inline void begin() { Bridge.spiBegin(); }
    inline void end() { Bridge.spiEnd(); }
    inline void setConfig(const rpc::payload::SpiConfig& config) { Bridge.spiSetConfig(config); }
    inline size_t transfer(etl::span<uint8_t> buffer) { return Bridge.spiTransfer(buffer); }
}
}

#endif
