#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "services/FileSystem.h"
#include "test_support.h"
#include <etl/array.h>

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

namespace {
using bridge::test::TestAccessor;

void test_fs_read_callback(etl::span<const uint8_t>) {}

void test_hal_roundtrip() {
  etl::array<uint8_t, 8> payload = {'m', 'c', 'u', '-', 'd', 'a', 't', 'a'};
  const etl::string_view path = "test_hal.bin";

  auto res_w = bridge::hal::writeFile(path, etl::span<const uint8_t>(payload.data(), payload.size()));
  TEST_ASSERT(res_w.has_value());

  etl::array<uint8_t, 32> read_buffer;
  read_buffer.fill(0);
  auto res_r = bridge::hal::readFileChunk(path, 0, etl::span<uint8_t>(read_buffer.data(), read_buffer.size()));
  TEST_ASSERT(res_r.has_value());
  TEST_ASSERT_EQUAL(payload.size(), res_r->bytes_read);
  TEST_ASSERT(etl::equal(payload.begin(), payload.end(), read_buffer.begin()));

  (void)bridge::hal::removeFile(path);
}

void test_hal_chunked_read_roundtrip() {
  const etl::string_view path = "test_chunks.bin";
  etl::array<uint8_t, 120> read_payload;
  for (size_t index = 0; index < read_payload.size(); ++index) {
    read_payload[index] = static_cast<uint8_t>('a' + (index % 26U));
  }

  auto res_w = bridge::hal::writeFile(path, etl::span<const uint8_t>(read_payload.data(), read_payload.size()));
  TEST_ASSERT(res_w.has_value());

  etl::array<uint8_t, 62> first_chunk;
  first_chunk.fill(0);
  etl::array<uint8_t, 62> second_chunk;
  second_chunk.fill(0);

  auto res_r1 = bridge::hal::readFileChunk(path, 0, etl::span<uint8_t>(first_chunk.data(), first_chunk.size()));
  TEST_ASSERT(res_r1.has_value());
  TEST_ASSERT_EQUAL(62, res_r1->bytes_read);
  TEST_ASSERT(res_r1->has_more);

  auto res_r2 = bridge::hal::readFileChunk(path, 62, etl::span<uint8_t>(second_chunk.data(), second_chunk.size()));
  TEST_ASSERT(res_r2.has_value());
  TEST_ASSERT_EQUAL(120 - 62, res_r2->bytes_read);
  TEST_ASSERT(!res_r2->has_more);

  TEST_ASSERT(etl::equal(read_payload.begin(), read_payload.begin() + 62, first_chunk.begin()));
  TEST_ASSERT(etl::equal(read_payload.begin() + 62, read_payload.end(), second_chunk.begin()));

  (void)bridge::hal::removeFile(path);
}

void test_filesystem_api_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  etl::array<uint8_t, 3> data = {1, 2, 3};
  FileSystem.write("api_write.bin", etl::span<const uint8_t>(data.data(), data.size()));
}

void test_filesystem_api_read() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  FileSystem.read("api_read.bin", FileSystemClass::FileSystemReadHandler::create<test_fs_read_callback>());
}

void test_filesystem_api_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  FileSystem.remove("api_rem.bin");
}

void test_filesystem_on_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  etl::array<uint8_t, 3> resp_data = {4, 5, 6};
  rpc::payload::FileWrite msg;
  msg.path = "on_write.bin";
  msg.data = etl::span<const uint8_t>(resp_data.data(), resp_data.size());
  FileSystem._onWrite(msg);
}

void test_filesystem_on_read() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  const etl::string_view path = "on_read.bin";
  etl::array<uint8_t, 1> data = {0xAA};
  (void)bridge::hal::writeFile(path, etl::span<const uint8_t>(data.data(), data.size()));

  rpc::payload::FileRead msg;
  msg.path = path;
  FileSystem._onRead(msg);
  (void)bridge::hal::removeFile(path);
}

void test_filesystem_on_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  rpc::payload::FileRemove msg;
  msg.path = "on_rem.bin";
  FileSystem._onRemove(msg);
}

void test_filesystem_observer() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  FileSystem.notification(MsgBridgeSynchronized{});
  FileSystem.notification(MsgBridgeLost{});
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_hal_roundtrip);
  RUN_TEST(test_hal_chunked_read_roundtrip);
  RUN_TEST(test_filesystem_api_write);
  RUN_TEST(test_filesystem_api_read);
  RUN_TEST(test_filesystem_api_remove);
  RUN_TEST(test_filesystem_on_write);
  RUN_TEST(test_filesystem_on_read);
  RUN_TEST(test_filesystem_on_remove);
  RUN_TEST(test_filesystem_observer);
  return UNITY_END();
}
