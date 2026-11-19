#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include "services/FileSystem.h"

#include <etl/string_view.h>
#include <pw_span/span.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

#include <etl/optional.h>
#include <pw_stream/memory_stream.h>
#include <pw_transfer/handler.h>

#include "protocol/rpc_protocol.h"

namespace {

// Read-only view over the sketch buffer of the upload in progress. The
// daemon may retry reads; the resource stays armed until the Upload call
// completes.
class UploadTransferHandler final : public pw::transfer::ReadOnlyHandler {
 public:
  UploadTransferHandler()
      : pw::transfer::ReadOnlyHandler(rpc::RPC_FILE_UPLOAD_RESOURCE_ID) {}

  void arm(pw::ConstByteSpan data) { data_ = data; }

  void disarm() {
    data_ = pw::ConstByteSpan();
    reader_.reset();
  }

  pw::Status PrepareRead() override {
    if (data_.empty()) return pw::Status::FailedPrecondition();
    reader_.emplace(data_);
    set_reader(*reader_);
    return pw::OkStatus();
  }

 private:
  pw::ConstByteSpan data_;
  etl::optional<pw::stream::MemoryReader> reader_;
};

UploadTransferHandler g_upload_handler;
pw::rpc::NanopbUnaryReceiver<rpc_pb_Empty> g_upload_call;
bool g_upload_pending = false;
pw::Status g_last_upload_status;

void finishUpload(pw::Status status) {
  g_upload_handler.disarm();
  g_last_upload_status = status;
  g_upload_pending = false;
}

}  // namespace

void FileSystemClass::_resetUpload() {
  g_upload_handler.disarm();
  g_upload_call = {};
  g_upload_pending = false;
}

bool FileSystemClass::registerUploadHandler(
    pw::transfer::TransferService& service) {
  return service.RegisterHandler(g_upload_handler);
}

bool FileSystemClass::uploadPending() { return g_upload_pending; }

pw::Status FileSystemClass::lastUploadStatus() { return g_last_upload_status; }

bool FileSystemClass::upload(etl::string_view path,
                             pw::span<const uint8_t> data) {
  const bool invalid = g_upload_pending || path.empty() || data.empty() ||
                       path.size() >= sizeof(rpc_pb_FileUpload{}.path) ||
                       data.size() > UINT32_MAX;
  if (invalid) return false;
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return false;
  }
  rpc_pb_FileUpload request = rpc_pb_FileUpload_init_default;
  bridge::pb::copy_string(request.path, sizeof(request.path), path);
  request.size = static_cast<uint32_t>(data.size());

  g_upload_handler.arm(pw::as_bytes(data));
  g_upload_pending = true;
  g_upload_call = Bridge.fileSystemClient().Upload(
      request,
      [](const rpc_pb_Empty&, pw::Status status) { finishUpload(status); },
      [](pw::Status status) { finishUpload(status); });
  return true;
}

void FileSystemClass::write(etl::string_view path,
                            pw::span<const uint8_t> data) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }
  rpc_pb_FileWrite p = rpc_pb_FileWrite_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);
  bridge::pb::copy_bytes(p.data, data);

  (void)Bridge.fileSystemClient().Write(p);
}

void FileSystemClass::remove(etl::string_view path) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }
  rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  (void)Bridge.fileSystemClient().Remove(p);
}

void FileSystemClass::_onWrite(const rpc_pb_FileWrite& msg) {
  bridge::hal::writeFile(
      etl::string_view(msg.path),
      pw::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

void FileSystemClass::_onRemove(const rpc_pb_FileRemove& msg) {
  bridge::hal::removeFile(etl::string_view(msg.path));
}

FileSystemType FileSystem;

#endif
