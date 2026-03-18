#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "hal/hal.h"
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

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_hal_roundtrip);
  RUN_TEST(test_hal_chunked_read_roundtrip);
  return UNITY_END();
}
