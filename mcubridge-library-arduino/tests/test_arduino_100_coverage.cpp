#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include <Arduino.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1

#include "Bridge.h"
#include "protocol/rle.h"
#include "services/SPIService.h"
#include "test_support.h"

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }
void delay(unsigned long ms) { g_test_millis += ms; }

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

void test_bridge_reset_state() {
  auto ba = bridge::test::TestAccessor::create(Bridge);
  Bridge.begin(115200);
  ba.setStartupStabilizing(false); // Move from STABILIZING to UNSYNCHRONIZED
  TEST_ASSERT(Bridge.isUnsynchronized());
}

void test_bridge_is_recent_duplicate_edge_cases() {
  auto ba = bridge::test::TestAccessor::create(Bridge);
  rpc::Frame f;
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.payload_length = 0;
  f.crc = 0x12345678;

  // First time: not a duplicate
  ba.clearRxHistory();
  TEST_ASSERT(!ba.isRecentDuplicateRx(f));

  // Mark as processed to add to history
  ba.markRxProcessed(f);

  // Second time: duplicate
  TEST_ASSERT(ba.isRecentDuplicateRx(f));

  // After history is cleared or changed, it should not be a duplicate
  f.crc = 0x87654321;
  TEST_ASSERT(!ba.isRecentDuplicateRx(f));
}

void test_console_write_extra_gaps() {
  // Mock a full stream to trigger retry paths if possible
  Console.begin();
  Console.write('t');
  Console.print("test");
  Console.println("line");
  Console.flush();

  // Test read path
  TEST_ASSERT(Console.available() == 0);
  auto ca = bridge::test::ConsoleTestAccessor::create(Console);
  ca.pushRxByte('X');
  TEST_ASSERT(Console.available() == 1);
  TEST_ASSERT(Console.read() == 'X');
}

void test_datastore_extra_gaps() {
  auto ba = bridge::test::TestAccessor::create(Bridge);
  rpc::Frame f;
  uint8_t buf[2];

  // Gap: handleResponse CMD_DATASTORE_GET_RESP without handler
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
  f.header.payload_length = 2;
  buf[0] = 1;
  buf[1] = 'V';
  f.payload = etl::span<const uint8_t>(buf, 2);
  ba.dispatch(f);

  // Gap: handleResponse with other command
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  ba.dispatch(f);
}

void test_mailbox_extra_gaps() {
  auto ba = bridge::test::TestAccessor::create(Bridge);
  rpc::Frame f;
  uint8_t buf[3];

  // Gap: handleResponse CMD_MAILBOX_READ_RESP without handler
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  f.header.payload_length = 3;
  rpc::write_u16_be(etl::span<uint8_t>(buf, 2), 1);
  buf[2] = 'M';
  f.payload = etl::span<const uint8_t>(buf, 3);
  ba.dispatch(f);

  // Gap: handleResponse CMD_MAILBOX_AVAILABLE_RESP without handler
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(etl::span<uint8_t>(buf, 2), 10);
  f.payload = etl::span<const uint8_t>(buf, 2);
  ba.dispatch(f);
}

void test_process_extra_gaps() {
  auto ba = bridge::test::TestAccessor::create(Bridge);
  rpc::Frame f;
  uint8_t buf[2];

  // Gap: handleResponse CMD_PROCESS_RUN_ASYNC_RESP with handler
  Process.runAsync("echo", etl::span<const etl::string_view>{},
      ProcessClass::ProcessRunAsyncHandler::create([](int16_t p) { (void)p; }));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(etl::span<uint8_t>(buf, 2), 456);
  f.payload = etl::span<const uint8_t>(buf, 2);
  ba.dispatch(f);

  // Gap: handleResponse CMD_PROCESS_POLL_RESP without handler
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  f.header.payload_length = 0;
  f.payload = etl::span<const uint8_t>();
  ba.dispatch(f);

  // Gap: handleResponse with other command
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  ba.dispatch(f);
}

void test_rle_gaps() {
  uint8_t src[10];
  uint8_t dst[10];
  // Gap: should_compress with small buffer
  TEST_ASSERT(!rle::should_compress(etl::span<const uint8_t>(src, 2)));
  // Gap: encode/decode empty/zero
  TEST_ASSERT_EQ_UINT(rle::encode(etl::span<const uint8_t>(src, 0),
                                  etl::span<uint8_t>(dst, 10)),
                      0);
  TEST_ASSERT_EQ_UINT(rle::decode(etl::span<const uint8_t>(src, 0),
                                  etl::span<uint8_t>(dst, 10)),
                      0);
}

void test_system_extra_gaps() {
  auto ba = bridge::test::TestAccessor::create(Bridge);
  rpc::Frame f;
  uint8_t buf[1];

  // Gap: handleResponse with unsupported system command
  f.header.command_id = 0x4F;
  f.header.payload_length = 1;
  buf[0] = 0;
  f.payload = etl::span<const uint8_t>(buf, 1);
  ba.dispatch(f);
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_reset_state);
  RUN_TEST(test_bridge_is_recent_duplicate_edge_cases);
  RUN_TEST(test_console_write_extra_gaps);
  RUN_TEST(test_datastore_extra_gaps);
  RUN_TEST(test_mailbox_extra_gaps);
  RUN_TEST(test_process_extra_gaps);
  RUN_TEST(test_rle_gaps);
  RUN_TEST(test_system_extra_gaps);
  return UNITY_END();
}
