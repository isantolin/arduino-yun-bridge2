#include "services/FileSystem.h"

#include <etl/algorithm.h>
#include <etl/fixed_iterator.h>
#include <etl/numeric.h>

#include "Bridge.h"

#if BRIDGE_ENABLE_FILESYSTEM

void FileSystemClass::write(etl::string_view path,
                            etl::span<const uint8_t> data) {
  rpc_pb_FileWrite p = rpc_pb_FileWrite_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);
  bridge::pb::copy_bytes(p.data, data);

  constexpr uint32_t kServiceId =
      pw::tokenizer::Hash("bridge.FileSystemService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Write");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_FileWrite_fields,
                             p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::read(
    etl::string_view path,
    typename FileSystemClass::FileSystemReadHandler handler) {
  _pending_reads.clear();
  rpc_pb_FileRead p = rpc_pb_FileRead_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  constexpr uint32_t kServiceId =
      pw::tokenizer::Hash("bridge.FileSystemService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Read");
  if (Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_FileRead_fields, p)) {
    _pending_reads.push(handler);
  } else {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::remove(etl::string_view path) {
  rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
  bridge::pb::copy_string(p.path, sizeof(p.path), path);

  constexpr uint32_t kServiceId =
      pw::tokenizer::Hash("bridge.FileSystemService");
  constexpr uint32_t kMethodId = pw::tokenizer::Hash("Remove");
  if (!Bridge.sendRpcRequest(kServiceId, kMethodId, rpc_pb_FileRemove_fields,
                             p)) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR);
  }
}

void FileSystemClass::_onWrite(const rpc_pb_FileWrite& msg) {
  bridge::hal::writeFile(
      etl::string_view(msg.path),
      etl::span<const uint8_t>(msg.data.bytes, msg.data.size));
}

void FileSystemClass::_onRead(const rpc_pb_FileRead& msg) {
  const etl::string_view path(msg.path);
  size_t offset = 0U;
  const uint32_t start_ms = millis();

  uint16_t chunk_count = 0U;
  uint8_t cursor = 0U;
  uint8_t sentinel = 0U;
  const auto it = etl::find_if(
      etl::fixed_iterator<uint8_t*>(&cursor),
      etl::fixed_iterator<uint8_t*>(&sentinel), [&](uint8_t) {
        if (++chunk_count > bridge::config::FILE_MAX_READ_CHUNKS) {
          return true;
        }
        if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS) {
          return true;
        }
        rpc_pb_FileReadResponse p = rpc_pb_FileReadResponse_init_default;
        const etl::span<uint8_t> chunk_buf(p.content.bytes,
                                           sizeof(p.content.bytes));
        auto res = bridge::hal::readFileChunk(path, offset, chunk_buf);
        if (!res) {
          return true;
        }

        p.content.size = static_cast<pb_size_t>(res->bytes_read);
        constexpr uint32_t kServiceId =
            pw::tokenizer::Hash("bridge.FileSystemService");
        constexpr uint32_t kMethodId = pw::tokenizer::Hash("Read");
        if (!Bridge.sendRpcRequest(kServiceId, kMethodId,
                                   rpc_pb_FileReadResponse_fields, p)) {
          return true;
        }

        if (!res->has_more) {
          rpc_pb_FileReadResponse empty_p =
              rpc_pb_FileReadResponse_init_default;
          (void)Bridge.sendRpcRequest(kServiceId, kMethodId,
                                      rpc_pb_FileReadResponse_fields, empty_p);
          return true;
        }

        offset += res->bytes_read;
        return false;
      });
  (void)it;
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
