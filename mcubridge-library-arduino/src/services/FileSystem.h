#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#include <stdint.h>
#include "config/bridge_config.h"
#undef min
#undef max
#include "etl/delegate.h"
#include "etl/span.h"
#include "etl/string_view.h"
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
  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { _read_handler.clear(); }

  void write(etl::string_view path, etl::span<const uint8_t> data);
  void read(etl::string_view path, FileSystemReadHandler handler);
  void remove(etl::string_view path);

  void _onWrite(const rpc::payload::FileWrite& msg);
  void _onResponse(const rpc::payload::FileReadResponse& msg);

 private:
  FileSystemReadHandler _read_handler;
};

#if BRIDGE_ENABLE_FILESYSTEM
extern FileSystemClass FileSystem;
#endif

#endif
