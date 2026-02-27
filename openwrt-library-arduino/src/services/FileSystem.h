#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_FILESYSTEM
#include "etl/delegate.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"

class BridgeClass;

class FileSystemClass {
  friend class BridgeClass;

 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass();

  void write(etl::string_view filePath, etl::span<const uint8_t> data);
  void remove(etl::string_view filePath);
  void read(etl::string_view filePath);

  inline void onFileSystemReadResponse(FileSystemReadHandler handler) {
    _file_system_read_handler = handler;
  }

 private:
  FileSystemReadHandler _file_system_read_handler;
};

extern FileSystemClass FileSystem;
#endif

#endif
