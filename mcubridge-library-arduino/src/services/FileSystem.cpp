#include "services/FileSystem.h"

#include <etl/span.h>
#include <etl/string_view.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_FileWrite p = rpc_pb_FileWrite_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);
  bridge::pb::copy_bytes(p.data, data);

  (void)Bridge.fileSystemClient().Write(p);
}

void FileSystemClass::read(
    etl::string_view path,
    typename FileSystemClass::FileSystemReadHandler handler) {
  _pending_reads.clear();
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_FileRead p = rpc_pb_FileRead_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  _read_call = Bridge.fileSystemClient().Read(
      p, [](const rpc_pb_FileReadResponse& resp, pw::Status status) {
        static_cast<void>(status);
        FileSystem._onResponse(resp);
      });
  _pending_reads.push(handler);
}

void FileSystemClass::remove(etl::string_view path) {
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
    return;
  }
  rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  (void)Bridge.fileSystemClient().Remove(p);
}

void FileSystemClass::_onWrite(const rpc_pb_FileWrite& msg) {
  bridge::hal::writeFile(
      etl::string_view(msg.path),
      etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

pw::Status FileSystemClass::_onRead(const rpc_pb_FileRead& msg,
                                    rpc_pb_FileReadResponse& response) {
  response = rpc_pb_FileReadResponse_init_default;
  const etl::span<uint8_t> chunk_buf(response.content.bytes,
                                     sizeof(response.content.bytes));
  const auto res =
      bridge::hal::readFileChunk(etl::string_view(msg.path), 0U, chunk_buf);
  if (!res) {
    return pw::Status::NotFound();
  }
  if (res->has_more) {
    response.content.size = 0U;
    return pw::Status::ResourceExhausted();
  }
  response.content.size = static_cast<pb_size_t>(res->bytes_read);
  return pw::OkStatus();
}

void FileSystemClass::_onRemove(const rpc_pb_FileRemove& msg) {
  bridge::hal::removeFile(etl::string_view(msg.path));
}

void FileSystemClass::_onResponse(const rpc_pb_FileReadResponse& msg) {
  if (_pending_reads.empty()) return;
  const FileSystemReadHandler handler = _pending_reads.front();
  if (msg.content.size == 0U) {
    _pending_reads.pop();
  }
  if (handler.is_valid()) {
    handler(etl::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

FileSystemType FileSystem;

#endif
