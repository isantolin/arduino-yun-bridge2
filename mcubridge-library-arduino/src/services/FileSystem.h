#ifndef SERVICES_FILESYSTEM_H
#define SERVICES_FILESYSTEM_H

#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include <etl/string_view.h>
#include <pw_span/span.h>

#include "Bridge.h"
#include "protocol/mcu_wire.pb.h"

class FileSystemClass {
 public:
  FileSystemClass() = default;
  static void write(etl::string_view path, pw::span<const uint8_t> data);
  static void remove(etl::string_view path);

  static void _onWrite(const rpc_pb_FileWrite& msg);
  static void _onRemove(const rpc_pb_FileRemove& msg);
};

using FileSystemType = FileSystemClass;
extern FileSystemType FileSystem;

#endif
