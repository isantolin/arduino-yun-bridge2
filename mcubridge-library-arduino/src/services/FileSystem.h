#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#undef min
#undef max
#include <etl/delegate.h>
#include <etl/span.h>
#include <etl/string_view.h>

#include "Bridge.h"
#include "protocol/PendingRpc.h"
#include "protocol/rpc_structs.h"

class FileSystemClass : public bridge::BridgeObserver {
 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass();
  static void write(etl::string_view path, etl::span<const uint8_t> data);
  void read(etl::string_view path, FileSystemReadHandler handler);
  static void remove(etl::string_view path);

  static void _onWrite(const rpc::payload::FileWrite& msg);
  static void _onRead(const rpc::payload::FileRead& msg);
  static void _onRemove(const rpc::payload::FileRemove& msg);
  void _onResponse(const rpc::payload::FileReadResponse& msg);

  void onLost() override { _pending_reads.clear(); }

  bridge::PendingRpcManager<rpc::payload::FileReadResponse, 1,
                            FileSystemReadHandler>
      _pending_reads;
};

using FileSystemType = FileSystemClass;
extern FileSystemType FileSystem;

#endif
