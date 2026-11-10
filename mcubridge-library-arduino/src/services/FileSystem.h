#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#undef min
#undef max
#include <etl/circular_buffer.h>
#include <etl/delegate.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <pw_status/status.h>

#include "Bridge.h"
#include "protocol/mcu_wire.pb.h"

class FileSystemClass : public bridge::BridgeObserver {
 public:
  using FileSystemReadHandler = etl::delegate<void(etl::span<const uint8_t>)>;

  FileSystemClass() = default;
  static void write(etl::string_view path, etl::span<const uint8_t> data);
  void read(etl::string_view path, FileSystemReadHandler handler);
  static void remove(etl::string_view path);

  static void _onWrite(const rpc_pb_FileWrite& msg);
  static pw::Status _onRead(
      const rpc_pb_FileRead& msg,
      pw::rpc::NanopbServerWriter<rpc_pb_FileReadResponse>& writer);
  static void _onRemove(const rpc_pb_FileRemove& msg);
  void _onResponse(const rpc_pb_FileReadResponse& msg);

  void onLost() override {
    _read_call = {};
    _pending_reads.clear();
  }

 private:
  pw::rpc::NanopbClientReader<rpc_pb_FileReadResponse> _read_call{};
  etl::circular_buffer<FileSystemReadHandler, 1> _pending_reads;
};

using FileSystemType = FileSystemClass;
extern FileSystemType FileSystem;

#endif
