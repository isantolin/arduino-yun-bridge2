#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "hal/hal.h"
#include "services/SPIService.h"
#include "test_support.h"

static unsigned long g_test_millis = 0;
unsigned long millis() { return ++g_test_millis; }

HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;
#if BRIDGE_ENABLE_SPI
SPIServiceClass SPIService;
#endif
Stream* g_arduino_stream_delegate = nullptr;

namespace {

void test_hal_roundtrip() {
  const uint8_t payload[] = {'m', 'c', 'u', '-', 'd', 'a', 't', 'a'};
  uint8_t read_buffer[32] = {};
  size_t bytes_read = 0;
  bool has_more = false;

  TEST_ASSERT_TRUE(bridge::hal::writeFile("hostfs/direct.bin", etl::span<const uint8_t>(payload, sizeof(payload))));
  TEST_ASSERT_TRUE(bridge::hal::readFileChunk(
      "hostfs/direct.bin",
      0U,
      etl::span<uint8_t>(read_buffer, sizeof(read_buffer)),
      bytes_read,
      has_more));
  TEST_ASSERT_EQUAL(sizeof(payload), bytes_read);
  TEST_ASSERT_FALSE(has_more);
  TEST_ASSERT_TRUE(test_memeq(payload, read_buffer, sizeof(payload)));
  TEST_ASSERT_TRUE(bridge::hal::removeFile("hostfs/direct.bin"));
}

void test_hal_chunked_read_roundtrip() {
  etl::array<uint8_t, 96> read_payload = {};
  for (size_t index = 0; index < read_payload.size(); ++index) {
    read_payload[index] = static_cast<uint8_t>('a' + (index % 26U));
  }
  TEST_ASSERT_TRUE(bridge::hal::writeFile(
      "hostfs/chunked.bin",
      etl::span<const uint8_t>(read_payload.data(), read_payload.size())));

  uint8_t first_chunk[62] = {};
  uint8_t second_chunk[62] = {};
  size_t bytes_read = 0;
  bool has_more = false;

  TEST_ASSERT_TRUE(bridge::hal::readFileChunk(
      "hostfs/chunked.bin",
      0U,
      etl::span<uint8_t>(first_chunk, sizeof(first_chunk)),
      bytes_read,
      has_more));
  TEST_ASSERT_EQUAL(62U, bytes_read);
  TEST_ASSERT_TRUE(has_more);
  TEST_ASSERT_TRUE(test_memeq(read_payload.data(), first_chunk, bytes_read));

  TEST_ASSERT_TRUE(bridge::hal::readFileChunk(
      "hostfs/chunked.bin",
      bytes_read,
      etl::span<uint8_t>(second_chunk, sizeof(second_chunk)),
      bytes_read,
      has_more));
  TEST_ASSERT_EQUAL(34U, bytes_read);
  TEST_ASSERT_FALSE(has_more);
  TEST_ASSERT_TRUE(test_memeq(read_payload.data() + 62U, second_chunk, bytes_read));
  TEST_ASSERT_TRUE(bridge::hal::removeFile("hostfs/chunked.bin"));
}

void test_filesystem_api_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint8_t data[] = {1, 2, 3};
  FileSystem.write("test.txt", etl::span<const uint8_t>(data, 3));
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_filesystem_api_read() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  bool called = false;
  FileSystem.read("test.txt", [&called](etl::span<const uint8_t> data) { called = true; });
  TEST_ASSERT(stream.tx_buf.len > 0);
  
  uint8_t resp_data[] = {4, 5, 6};
  FileSystem._onResponse(etl::span<const uint8_t>(resp_data, 3));
  TEST_ASSERT(called);
}

void test_filesystem_api_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  FileSystem.remove("test.txt");
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_filesystem_on_write() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::payload::FileWrite msg = {};
  strcpy(msg.path, "hostfs/write.bin");
  uint8_t data[] = {0xAA};
  FileSystem._onWrite(msg, etl::span<const uint8_t>(data, 1));
  TEST_ASSERT(stream.tx_buf.len > 0);
  
  // Failure case
  strcpy(msg.path, "hostfs/nonexistent_dir/write.bin");
  FileSystem._onWrite(msg, etl::span<const uint8_t>(data, 1));
}

void test_filesystem_on_read() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Create a file to read
  const uint8_t payload[] = {'x', 'y', 'z'};
  bridge::hal::writeFile("hostfs/read.bin", etl::span<const uint8_t>(payload, sizeof(payload)));

  rpc::payload::FileRead msg = {};
  strcpy(msg.path, "hostfs/read.bin");
  FileSystem._onRead(msg);
  TEST_ASSERT(stream.tx_buf.len > 0);

  // Failure case
  strcpy(msg.path, "hostfs/missing.bin");
  FileSystem._onRead(msg);
}

void test_filesystem_on_remove() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Create file to remove
  bridge::hal::writeFile("hostfs/remove.bin", etl::span<const uint8_t>());
  
  rpc::payload::FileRemove msg = {};
  strcpy(msg.path, "hostfs/remove.bin");
  FileSystem._onRemove(msg);
  TEST_ASSERT(stream.tx_buf.len > 0);

  // Failure case
  strcpy(msg.path, "hostfs/missing.bin");
  FileSystem._onRemove(msg);
}

void test_filesystem_observer() {
  FileSystem.notification(MsgBridgeSynchronized{});
  FileSystem.notification(MsgBridgeLost{});
  FileSystem.notification(MsgBridgeError{rpc::StatusCode::STATUS_ERROR});
  FileSystem.notification(MsgBridgeCommand{});
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
