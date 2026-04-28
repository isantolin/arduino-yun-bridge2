#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "hal/hal.h"
#include "services/SPIService.h"
#include "services/FileSystem.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "test_support.h"

static unsigned long g_test_millis = 0;
unsigned long millis() { return ++g_test_millis; }

// Bridge and core services are already provided by production code.
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

void test_hal_roundtrip() {
  const uint8_t payload[] = {'m', 'c', 'u', '-', 'd', 'a', 't', 'a'};
  uint8_t read_buffer[32] = {};

  TEST_ASSERT_TRUE(bridge::hal::writeFile("hostfs/direct.bin", etl::span<const uint8_t>(payload, sizeof(payload))).has_value());
  auto res = bridge::hal::readFileChunk(
      "hostfs/direct.bin",
      0U,
      etl::span<uint8_t>(read_buffer, sizeof(read_buffer)));
  
  TEST_ASSERT_TRUE(res.has_value());
  TEST_ASSERT_EQUAL(sizeof(payload), res.value().bytes_read);
  TEST_ASSERT_FALSE(res.value().has_more);
  TEST_ASSERT_TRUE(etl::equal(payload, payload + sizeof(payload), read_buffer));
  TEST_ASSERT_TRUE(bridge::hal::removeFile("hostfs/direct.bin").has_value());
}

void test_hal_chunked_read_roundtrip() {
  etl::array<uint8_t, 96> read_payload = {};
  for (size_t index = 0; index < read_payload.size(); ++index) {
    read_payload[index] = static_cast<uint8_t>('a' + (index % 26U));
  }
  TEST_ASSERT_TRUE(bridge::hal::writeFile(
      "hostfs/chunked.bin",
      etl::span<const uint8_t>(read_payload.data(), read_payload.size())).has_value());

  uint8_t first_chunk[62] = {};
  uint8_t second_chunk[62] = {};

  auto res1 = bridge::hal::readFileChunk(
      "hostfs/chunked.bin",
      0U,
      etl::span<uint8_t>(first_chunk, sizeof(first_chunk)));
  
  TEST_ASSERT_TRUE(res1.has_value());
  TEST_ASSERT_EQUAL(62U, res1.value().bytes_read);
  TEST_ASSERT_TRUE(res1.value().has_more);
  TEST_ASSERT_TRUE(etl::equal(read_payload.begin(), read_payload.begin() + res1.value().bytes_read, first_chunk));

  auto res2 = bridge::hal::readFileChunk(
      "hostfs/chunked.bin",
      res1.value().bytes_read,
      etl::span<uint8_t>(second_chunk, sizeof(second_chunk)));
  
  TEST_ASSERT_TRUE(res2.has_value());
  TEST_ASSERT_EQUAL(34U, res2.value().bytes_read);
  TEST_ASSERT_FALSE(res2.value().has_more);
  TEST_ASSERT_TRUE(etl::equal(read_payload.begin() + 62U, read_payload.begin() + 62U + res2.value().bytes_read, second_chunk));
  TEST_ASSERT_TRUE(bridge::hal::removeFile("hostfs/chunked.bin").has_value());
}

void test_filesystem_api_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint8_t data[] = {1, 2, 3};
  FileSystem.write("test.txt", etl::span<const uint8_t>(data, 3));
  TEST_ASSERT(stream.tx_buf.len > 0);
}

static bool g_filesystem_read_called = false;
void filesystem_test_read_handler(etl::span<const uint8_t> data) {
  (void)data;
  g_filesystem_read_called = true;
}

void test_filesystem_api_read() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  g_filesystem_read_called = false;
  FileSystem.read("test.txt", FileSystemClass::FileSystemReadHandler::create<filesystem_test_read_handler>());
  TEST_ASSERT(stream.tx_buf.len > 0);
  
  rpc::payload::FileReadResponse resp = {};
  uint8_t resp_data[] = {4, 5, 6};
  resp.content = etl::span<const uint8_t>(resp_data, 3);
  FileSystem._onResponse(resp);
  TEST_ASSERT(g_filesystem_read_called);
}

void test_filesystem_api_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  FileSystem.remove("test.txt");
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_filesystem_on_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::payload::FileWrite msg = {};
  msg.path = "hostfs/write.bin";
  uint8_t data[] = {0xAA};
  msg.data = etl::span<const uint8_t>(data, 1);
  FileSystem._onWrite(msg);
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_filesystem_on_read() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  const uint8_t payload[] = {'x', 'y', 'z'};
  bridge::hal::writeFile("hostfs/read.bin", etl::span<const uint8_t>(payload, sizeof(payload)));

  rpc::payload::FileRead msg = {};
  msg.path = "hostfs/read.bin";
  FileSystem._onRead(msg);
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_filesystem_on_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  bridge::hal::writeFile("hostfs/remove.bin", etl::span<const uint8_t>());
  
  rpc::payload::FileRemove msg = {};
  msg.path = "hostfs/remove.bin";
  FileSystem._onRemove(msg);
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_filesystem_observer() {
  FileSystem.notification(MsgBridgeSynchronized{});
  FileSystem.notification(MsgBridgeLost{});
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
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
