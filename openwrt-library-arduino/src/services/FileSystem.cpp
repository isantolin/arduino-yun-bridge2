#include "services/FileSystem.h"

#include "Bridge.h"
#include "etl/vector.h"
#include "protocol/PacketBuilder.h"

#if BRIDGE_ENABLE_FILESYSTEM

FileSystemClass::FileSystemClass() {}

void FileSystemClass::write(etl::string_view filePath, const uint8_t* data,
                            size_t length) {
  if (filePath.empty() || !data) return;

  if (filePath.length() > rpc::RPC_MAX_FILEPATH_LENGTH - 1) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
    return;
  }

  etl::vector<uint8_t, rpc::RPC_MAX_FILEPATH_LENGTH + 3> header;
  rpc::PacketBuilder builder(header);
  builder.add_pascal_string(filePath);
  builder.add_u16(static_cast<uint16_t>(length));

  Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE, header.data(),
                         header.size(), data, length);
}

void FileSystemClass::remove(etl::string_view filePath) {
  if (filePath.empty()) return;
  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_REMOVE, filePath,
                                rpc::RPC_MAX_FILEPATH_LENGTH - 1)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
  }
}

void FileSystemClass::read(etl::string_view filePath) {
  if (filePath.empty()) return;
  if (!Bridge.sendStringCommand(rpc::CommandId::CMD_FILE_READ, filePath,
                                rpc::RPC_MAX_FILEPATH_LENGTH - 1)) {
    Bridge._emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
  }
}

#endif
