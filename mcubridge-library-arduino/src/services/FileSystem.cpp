#include "services/FileSystem.h"

#include <etl/algorithm.h>
#include <etl/fixed_iterator.h>
#include <etl/string_view.h>
#include <pw_span/span.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

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

void FileSystemClass::read(
    etl::string_view path,
    typename FileSystemClass::FileSystemReadHandler handler) {
  _pending_reads.clear();
  if (!Bridge.isRpcTxReady()) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_UNAVAILABLE);
    return;
  }
  rpc_pb_FileRead p = rpc_pb_FileRead_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  _read_call = Bridge.fileSystemClient().Read(
      p,
      [](const rpc_pb_FileReadResponse& resp) { FileSystem._onResponse(resp); },
      [](pw::Status status) {
        static_cast<void>(status);
        FileSystem._pending_reads.clear();
      });
  _pending_reads.push(handler);
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

pw::Status FileSystemClass::_onRead(
    const rpc_pb_FileRead& msg,
    pw::rpc::NanopbServerWriter<rpc_pb_FileReadResponse>& writer) {
  const etl::string_view path(msg.path);
  size_t offset = 0U;
  uint8_t cursor = 0U;
  uint8_t sentinel = 0U;
  pw::Status result = pw::Status::ResourceExhausted();

  (void)etl::find_if(
      etl::fixed_iterator<uint8_t*>(&cursor),
      etl::fixed_iterator<uint8_t*>(&sentinel), [&](uint8_t) {
        rpc_pb_FileReadResponse response = rpc_pb_FileReadResponse_init_default;
        const pw::span<uint8_t> chunk_buf(response.content.bytes,
                                          sizeof(response.content.bytes));
        const auto res = bridge::hal::readFileChunk(path, offset, chunk_buf);
        if (!res.ok()) {
          result = pw::Status::NotFound();
          return true;
        }
        response.content.size = static_cast<pb_size_t>(res->bytes_read);
        if (response.content.size > 0U && !writer.Write(response).ok()) {
          result = pw::Status::Unavailable();
          return true;
        }
        if (!res->has_more) {
          result = pw::OkStatus();
          return true;
        }
        offset += res->bytes_read;
        return false;
      });

  (void)writer.Finish(result);
  return result;
}

void FileSystemClass::_onRemove(const rpc_pb_FileRemove& msg) {
  bridge::hal::removeFile(etl::string_view(msg.path));
}

void FileSystemClass::_onResponse(const rpc_pb_FileReadResponse& msg) {
  if (_pending_reads.empty()) return;
  const FileSystemReadHandler handler = _pending_reads.front();
  if (handler.is_valid()) {
    handler(pw::span<const uint8_t>(msg.content.bytes, msg.content.size));
  }
}

FileSystemType FileSystem;

#endif
