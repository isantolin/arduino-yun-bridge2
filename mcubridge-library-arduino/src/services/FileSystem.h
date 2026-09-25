#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#undef min
#undef max
#include <etl/delegate.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <etl/utility.h>
#include "Bridge.h"
#include "protocol/rpc_structs.h"

class FileSystemClass : public bridge::BridgeObserver {
 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass();

  void write(etl::string_view path, etl::span<const uint8_t> data);
  void read(etl::string_view path, FileSystemReadHandler handler);
  static void remove(etl::string_view path);

  template <auto HalFn, auto ReasonStr, typename... Args>
  static inline void _executeHalAction(Args&&... args) {
    if (!HalFn(etl::forward<Args>(args)...)) {
      Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR,
                        etl::string_view(ReasonStr));
    }
  }

  static void _onWrite(const rpc::payload::FileWrite& msg);
  static void _onRead(const rpc::payload::FileRead& msg);
  static void _onRemove(const rpc::payload::FileRemove& msg);
  void _onResponse(const rpc::payload::FileReadResponse& msg);

  void onLost() override { _read_handler = FileSystemReadHandler{}; }

 private:
  FileSystemReadHandler _read_handler;
};

using FileSystemType = FileSystemClass;
extern FileSystemType FileSystem;

#endif
