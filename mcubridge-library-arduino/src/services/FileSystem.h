#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#include "config/bridge_config.h"

#if BRIDGE_ENABLE_FILESYSTEM
#include "etl/delegate.h"
#include "etl/span.h"
#include "etl/string_view.h"
#include "protocol/rpc_protocol.h"
#include "router/command_router.h"
#include "etl/message_router.h"

class FileSystemClass : public etl::imessage_router {
 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass();

  // [SIL-2] imessage_router interface
  void receive(const etl::imessage& msg) override;
  bool accepts(etl::message_id_t id) const override;
  bool is_null_router() const override { return false; }
  bool is_producer() const override { return true; }
  bool is_consumer() const override { return true; }

  void write(etl::string_view filePath, etl::span<const uint8_t> data);
  void remove(etl::string_view filePath);
  void read(etl::string_view filePath);

  inline void onFileSystemReadResponse(FileSystemReadHandler handler) { _file_system_read_handler = handler; }

  FileSystemReadHandler _file_system_read_handler;
};

extern FileSystemClass FileSystem;
#endif
#endif
