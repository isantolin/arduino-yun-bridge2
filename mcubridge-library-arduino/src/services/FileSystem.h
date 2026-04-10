#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/string_view.h>
#include <etl/span.h>
#include <etl/delegate.h>
#include "protocol/BridgeEvents.h"
#include "protocol/rpc_structs.h"

class FileSystemClass {
 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass();
  static void write(etl::string_view path, etl::span<const uint8_t> data);
  void read(etl::string_view path, FileSystemReadHandler handler);
  [[maybe_unused]] static void remove(etl::string_view path);

  static void _onWrite(const rpc::payload::FileWrite& msg);
  void _onRead(const rpc::payload::FileRead& msg);
  static void _onRemove(const rpc::payload::FileRemove& msg);
  void _onResponse(const rpc::payload::FileReadResponse& msg);

  static void notification(MsgBridgeSynchronized) { /* ready */ }
  static void notification(MsgBridgeLost) { /* cleanup */ }

 private:
  FileSystemReadHandler _read_handler;
};

extern FileSystemClass FileSystem;

#endif
