#include "services/FileSystem.h"
#include "Bridge.h"
#include "pb_encode.h"
#include "pb_decode.h"
#if BRIDGE_ENABLE_FILESYSTEM
namespace {
constexpr size_t kReadChunkSize = 64U;
void send_read_response(etl::span<const uint8_t> content) {
  rpc_pb_FileReadResponse p = rpc_pb_FileReadResponse_init_default;
  p.content.funcs.encode = &BridgeClass::_encode_span_callback;
  p.content.arg = (void*)&content;
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 0, p);
}
}
FileSystemClass::FileSystemClass() {}
void FileSystemClass::write(etl::string_view path, etl::span<const uint8_t> data) {
  rpc_pb_FileWrite p = rpc_pb_FileWrite_init_default;
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) { etl::copy_n(path.begin(), p_copy, p.path); }
  p.path[p_copy] = '\0';
  p.data.funcs.encode = &BridgeClass::_encode_span_callback;
  p.data.arg = (void*)&data;
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_FILE_WRITE, 0, p);
}
void FileSystemClass::read(etl::string_view path, FileSystemReadHandler handler) {
  _read_handler = handler;
  rpc_pb_FileRead p = rpc_pb_FileRead_init_default;
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) { etl::copy_n(path.begin(), p_copy, p.path); }
  p.path[p_copy] = '\0';
  if (!Bridge.send(rpc::CommandId::CMD_FILE_READ, 0, p)) { Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR); }
}
void FileSystemClass::remove(etl::string_view path) {
  rpc_pb_FileRemove p = rpc_pb_FileRemove_init_default;
  const size_t p_copy = etl::min(path.size(), sizeof(p.path) - 1U);
  if (p_copy > 0U) { etl::copy_n(path.begin(), p_copy, p.path); }
  p.path[p_copy] = '\0';
  [[maybe_unused]] auto _u1 = Bridge.send(rpc::CommandId::CMD_FILE_REMOVE, 0, p);
}
void FileSystemClass::_onRead(const rpc::payload::FileRead& msg) {
  size_t offset = 0; etl::array<uint8_t, kReadChunkSize> buffer;
  const uint32_t start_ms = millis();
  for (uint16_t i = 0; i < bridge::config::FILE_MAX_READ_CHUNKS; ++i) {
    if (millis() - start_ms >= bridge::config::SERIAL_TIMEOUT_MS) break;
    auto res = bridge::hal::readFileChunk(etl::string_view(msg.path), offset, etl::span<uint8_t>(buffer.data(), buffer.size()));
    if (!res) { [[maybe_unused]] auto _u1 = Bridge.sendFrame(rpc::StatusCode::STATUS_ERROR); break; }
    send_read_response(etl::span<const uint8_t>(buffer.data(), res->bytes_read));
    if (!res->has_more) { send_read_response(etl::span<const uint8_t>()); break; }
    offset += res->bytes_read;
  }
}
void FileSystemClass::_onRemove(const rpc::payload::FileRemove& msg) {
  auto res = bridge::hal::removeFile(etl::string_view(msg.path));
  [[maybe_unused]] auto _u1 = Bridge.sendFrame(res ? rpc::StatusCode::STATUS_OK : rpc::StatusCode::STATUS_ERROR);
}
void FileSystemClass::_onResponse(etl::span<const uint8_t> content) { if (_read_handler.is_valid()) { _read_handler(content); } }
FileSystemClass FileSystem;
#endif
