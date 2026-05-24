#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#include "config/bridge_config.h"
#undef min
#undef max
#include <etl/string_view.h>
#include <etl/span.h>
#include <etl/delegate.h>
#include "protocol/BridgeEvents.h"
#include "protocol/mcubridge.pb.h"

class FileSystemClass : public BridgeObserver {
 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass();
  static void write(etl::string_view path, etl::span<const uint8_t> data);
  void read(etl::string_view path, FileSystemReadHandler handler);
  static void remove(etl::string_view path);

  static void _onWrite(const rpc_pb_FileWrite& msg);
  void _onRead(const rpc_pb_FileRead& msg);
  static void _onRemove(const rpc_pb_FileRemove& msg);
  void _onResponse(const rpc_pb_FileReadResponse& msg);

  void notification(MsgBridgeSynchronized) override { /* ready */ }
  void notification(MsgBridgeLost) override { /* cleanup */ }

 private:
  FileSystemReadHandler _read_handler;
};

extern FileSystemClass FileSystem;

#endif
