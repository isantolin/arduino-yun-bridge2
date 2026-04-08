#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"
#include "services/FileSystem.h"
#include "services/Process.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/SPIService.h"
#include "services/Console.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {
BiStream g_null_stream;
}

// Bridge and core services are already provided by production code.
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using bridge::test::TestAccessor;

static void reset_bridge_with_stream(BiStream& stream) {
  reset_bridge_core(Bridge, stream);
  Console.begin();
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();
}

static void restore_bridge_to_serial() {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(Serial);
}

// --- COVERAGE TESTS ---

static void test_all_handlers_coverage() {
  BiStream stream;
  reset_bridge_with_stream(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;

  // 1. Send all commands with empty payload to trigger MALFORMED / edge cases
  for (uint16_t cmd = 0; cmd < 255; cmd++) {
    f.header.command_id = cmd;
    f.payload = etl::span<const uint8_t>();
    ba.dispatch(f);
  }

  // 2. Exhaustive valid payload generation using macro
#define COVER_CMD(CMD_NAME, STRUCT_NAME) do { \
  rpc::payload::STRUCT_NAME msg = {}; \
  msgpack::Encoder enc(payload_buffer.data(), payload_buffer.size()); \
  msg.encode(enc); \
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_##CMD_NAME); \
  f.header.payload_length = static_cast<uint16_t>(enc.size()); \
  f.payload = etl::span<const uint8_t>(payload_buffer.data(), enc.size()); \
  ba.dispatch(f); \
} while(0)

  // Status
  // Handled specifically in error branches.

  // System
  COVER_CMD(GET_VERSION_RESP, VersionResponse);
  COVER_CMD(GET_FREE_MEMORY_RESP, FreeMemoryResponse);
  COVER_CMD(GET_CAPABILITIES_RESP, Capabilities);
  COVER_CMD(SET_BAUDRATE, SetBaudratePacket);
  COVER_CMD(ENTER_BOOTLOADER, EnterBootloader);
  COVER_CMD(LINK_SYNC, LinkSync);

  // GPIO
  COVER_CMD(SET_PIN_MODE, PinMode);
  COVER_CMD(DIGITAL_WRITE, DigitalWrite);
  COVER_CMD(ANALOG_WRITE, AnalogWrite);
  COVER_CMD(DIGITAL_READ, PinRead);
  COVER_CMD(ANALOG_READ, PinRead);
  COVER_CMD(DIGITAL_READ_RESP, DigitalReadResponse);
  COVER_CMD(ANALOG_READ_RESP, AnalogReadResponse);

  // Console
  COVER_CMD(CONSOLE_WRITE, ConsoleWrite);

  // Datastore
  COVER_CMD(DATASTORE_PUT, DatastorePut);
  COVER_CMD(DATASTORE_GET, DatastoreGet);
  COVER_CMD(DATASTORE_GET_RESP, DatastoreGetResponse);

  // Mailbox
  COVER_CMD(MAILBOX_PUSH, MailboxPush);
  COVER_CMD(MAILBOX_PROCESSED, MailboxProcessed);
  COVER_CMD(MAILBOX_AVAILABLE_RESP, MailboxAvailableResponse);
  COVER_CMD(MAILBOX_READ_RESP, MailboxReadResponse);

  // FileSystem
  COVER_CMD(FILE_WRITE, FileWrite);
  COVER_CMD(FILE_READ, FileRead);
  COVER_CMD(FILE_REMOVE, FileRemove);
  COVER_CMD(FILE_READ_RESP, FileReadResponse);

  // Process
  COVER_CMD(PROCESS_RUN_ASYNC, ProcessRunAsync);
  COVER_CMD(PROCESS_RUN_ASYNC_RESP, ProcessRunAsyncResponse);
  COVER_CMD(PROCESS_POLL, ProcessPoll);
  COVER_CMD(PROCESS_POLL_RESP, ProcessPollResponse);
  COVER_CMD(PROCESS_KILL, ProcessKill);

  // SPI
  COVER_CMD(SPI_TRANSFER, SpiTransfer);
  COVER_CMD(SPI_TRANSFER_RESP, SpiTransferResponse);
  COVER_CMD(SPI_SET_CONFIG, SpiConfig);

#undef COVER_CMD

  bridge::router::CommandContext ctx(&f, 0, 0, false, false);
  ba.handleGetVersion(ctx);
  ba.handleGetFreeMemory(ctx);

  restore_bridge_to_serial();
}

static bool async_called = false;
static void dummy_async_handler(int32_t) { async_called = true; }

static bool poll_called = false;
static void dummy_poll_handler(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>) { poll_called = true; }

static void test_process_api() {
  BiStream stream;
  reset_bridge_with_stream(stream);
  
  async_called = false;
  poll_called = false;

  Process.runAsync("ls", {}, etl::delegate<void(int32_t)>::create<dummy_async_handler>());
  Process.poll(1, etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>::create<dummy_poll_handler>());
  
  Process.kill(1);
  
  // Simulate responses
  rpc::payload::ProcessRunAsyncResponse msg_run = {};
  msg_run.pid = 1;
  Process._onRunAsyncResponse(msg_run);
  
  rpc::payload::ProcessPollResponse msg_poll = {};
  msg_poll.status = rpc::to_underlying(rpc::StatusCode::STATUS_OK);
  msg_poll.exit_code = 0;
  Process._onPollResponse(msg_poll);
  
  rpc::payload::ProcessKill msg_kill = {1};
  Process._kill(msg_kill);
  
  Process.reset();
  
  restore_bridge_to_serial();
}

static void test_console_api() {
  BiStream stream;
  reset_bridge_with_stream(stream);
  
  Console.begin();
  Console.write('a');
  uint8_t buf[] = {'b', 'c'};
  Console.write(buf, 2);
  Console.flush();
  
  rpc::payload::ConsoleWrite msg = {};
  uint8_t d[] = {'b', 'c'};
  msg.data = etl::span<const uint8_t>(d, 2);
  Console._push(msg);
  TEST_ASSERT_EQUAL(2, Console.available());
  TEST_ASSERT_EQUAL('b', Console.peek());
  TEST_ASSERT_EQUAL('b', Console.read());
  TEST_ASSERT_EQUAL(1, Console.available());
  Console.read();
  TEST_ASSERT_EQUAL(-1, Console.read());

  restore_bridge_to_serial();
}

static void test_datastore_api() {
  BiStream stream;
  reset_bridge_with_stream(stream);
  
#if BRIDGE_ENABLE_DATASTORE
  uint8_t data[] = {1, 2, 3};
  DataStore.set("test_key", etl::span<const uint8_t>(data, 3));
  DataStore.get("test_key", etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>());
  rpc::payload::DatastoreGetResponse resp = {};
  resp.value = etl::span<const uint8_t>(data, 3);
  DataStore._onResponse(resp);
#endif
  
  restore_bridge_to_serial();
}

static void test_mailbox_api() {
  BiStream stream;
  reset_bridge_with_stream(stream);

#if BRIDGE_ENABLE_MAILBOX
  uint8_t data[] = {4, 5, 6};
  Mailbox.push(etl::span<const uint8_t>(data, 3));
  Mailbox.requestRead();
  Mailbox.requestAvailable();
  
  rpc::payload::MailboxPush msg_push = {};
  msg_push.data = etl::span<const uint8_t>(data, 3);
  Mailbox._onIncomingData(msg_push);
  
  rpc::payload::MailboxReadResponse msg_read = {};
  msg_read.content = etl::span<const uint8_t>(data, 3);
  Mailbox._onIncomingData(msg_read);
  
  rpc::payload::MailboxAvailableResponse msg_avail = {};
  msg_avail.count = 2;
  Mailbox._onAvailableResponse(msg_avail);
#endif

  restore_bridge_to_serial();
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_all_handlers_coverage);
  RUN_TEST(test_process_api);
  RUN_TEST(test_console_api);
  RUN_TEST(test_datastore_api);
  RUN_TEST(test_mailbox_api);
  return UNITY_END();
}
