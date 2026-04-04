#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#include <stdint.h>
#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/delegate.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

#if defined(BRIDGE_HOST_TEST)
namespace bridge { namespace test { class FileSystemTestAccessor; } }
#endif

class FileSystemClass : public BridgeObserver {
#if defined(BRIDGE_HOST_TEST)
  friend class bridge::test::FileSystemTestAccessor;
#endif
 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass();

  // [SIL-2] Observer Interface
  [[maybe_unused]] void notification(MsgBridgeSynchronized) override { /* ready */ }
  [[maybe_unused]] void notification(MsgBridgeLost) override { _read_handler.clear(); }

  void write(etl::string_view path, etl::span<const uint8_t> data);
  void read(etl::string_view path, FileSystemReadHandler handler);
  [[maybe_unused]] void remove(etl::string_view path);

  void _onWrite(const rpc::payload::FileWrite& msg, etl::span<const uint8_t> data);
  void _onRead(const rpc::payload::FileRead& msg);
  void _onRemove(const rpc::payload::FileRemove& msg);
  void _onResponse(etl::span<const uint8_t> content);

 private:
  FileSystemReadHandler _read_handler;
};

#if BRIDGE_ENABLE_FILESYSTEM
extern FileSystemClass FileSystem;
#endif

#endif
