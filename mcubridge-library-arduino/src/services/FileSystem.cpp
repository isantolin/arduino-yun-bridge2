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
#include "services/CooperativeTransferHandler.h"

namespace {

bool g_upload_handler_destroyed = false;

// Read-only view over the sketch buffer of the upload in progress. The
// daemon may retry reads; the resource stays armed until the Upload call
// completes.
class UploadTransferHandler final : public pw::transfer::ReadOnlyHandler {
 public:
  UploadTransferHandler()
      : pw::transfer::ReadOnlyHandler(rpc::RPC_FILE_UPLOAD_RESOURCE_ID) {
    reader_.emplace(pw::ConstByteSpan());
    set_reader(*reader_);
  }

  ~UploadTransferHandler() override {
    registration_.detach(*this);
    g_upload_handler_destroyed = true;
  }

  bool attach(pw::transfer::TransferService& service,
              pw::transfer::TransferThread& thread) {
    return registration_.attach(*this, service, thread);
  }

  bool detachFrom(const pw::transfer::TransferService& service) {
    return registration_.detachFrom(*this, service);
  }

  void arm(pw::ConstByteSpan data) { data_ = data; }

  void disarm() {
    data_ = pw::ConstByteSpan();
    // Keep a live empty MemoryReader in-place so an in-flight pw::transfer
    // ServerContext cannot invoke pure-virtual Stream::DoSeek/DoRead if a
    // retry timeout fires before the session closes.
    reader_.emplace(pw::ConstByteSpan());
  }

  pw::Status PrepareRead() override {
    if (data_.empty()) return pw::Status::FailedPrecondition();
    reader_.emplace(data_);
    set_reader(*reader_);
    return pw::OkStatus();
  }

 private:
  bridge::services::CooperativeTransferRegistration registration_;
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
  if (g_upload_pending) {
    g_last_upload_status = pw::Status::Aborted();
  }
  g_upload_pending = false;
}

bool FileSystemClass::registerUploadHandler(
    pw::transfer::TransferService& service,
    pw::transfer::TransferThread& thread) {
  return g_upload_handler.attach(service, thread);
}

void FileSystemClass::unregisterUploadHandler(
    const pw::transfer::TransferService& service) {
  if (g_upload_handler_destroyed) return;
  if (g_upload_handler.detachFrom(service)) {
    _resetUpload();
  }
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
    Bridge.emitStatus(pw::Status::Unavailable());
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

void FileSystemClass::remove(etl::string_view path) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(pw::Status::Unavailable());
    return;
  }
  rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  (void)Bridge.fileSystemClient().Remove(p);
}

void FileSystemClass::_onRemove(const rpc_pb_FileRemove& msg) {
  bridge::hal::removeFile(etl::string_view(msg.path));
}

FileSystemType FileSystem;

#endif
