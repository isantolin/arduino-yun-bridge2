#include "services/FileSystem.h"

#include "Bridge.h"
#include "etl/vector.h"

#if BRIDGE_ENABLE_FILESYSTEM

FileSystemClass::FileSystemClass() = default;

void FileSystemClass::write(etl::string_view filePath,
                            etl::span<const uint8_t> data) {
  if (filePath.empty() || data.empty()) return;

  if (filePath.length() > rpc::RPC_MAX_FILEPATH_LENGTH - 1) {
    Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  uint8_t buffer[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t stream = pb_ostream_from_buffer(buffer, sizeof(buffer));

  rpc::payload::FileWrite msg = {};
  etl::copy_n(filePath.data(), filePath.length(), msg.path);
  msg.data.size = static_cast<pb_size_t>(etl::min<size_t>(data.size(), sizeof(msg.data.bytes)));
  etl::copy_n(data.begin(), msg.data.size, msg.data.bytes);

  if (pb_encode(&stream, rpc::Payload::Descriptor<rpc::payload::FileWrite>::fields(), &msg)) {
    static_cast<void>(Bridge.sendFrame(rpc::CommandId::CMD_FILE_WRITE,
                         etl::span<const uint8_t>(buffer, stream.bytes_written)));
  }
}

void FileSystemClass::remove(etl::string_view filePath) {
  static_cast<void>(Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_REMOVE, filePath,
                                rpc::RPC_MAX_FILEPATH_LENGTH - 1));
}

void FileSystemClass::read(etl::string_view filePath) {
  static_cast<void>(Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_READ, filePath,
                                rpc::RPC_MAX_FILEPATH_LENGTH - 1));
}

#endif
