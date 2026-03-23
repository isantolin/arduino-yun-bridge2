#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/mcubridge.pb.h"
#include "test_support.h"
#include "services/FileSystem.h"
#include "services/Process.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/SPIService.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {
BiStream g_null_stream;
}

BridgeClass Bridge(g_null_stream);
ConsoleClass Console;
#if BRIDGE_ENABLE_DATASTORE
DataStoreClass DataStore;
#endif
#if BRIDGE_ENABLE_MAILBOX
MailboxClass Mailbox;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
FileSystemClass FileSystem;
#endif
#if BRIDGE_ENABLE_PROCESS
ProcessClass Process;
#endif
#if BRIDGE_ENABLE_SPI
SPIServiceClass SPIService;
#endif
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
  bridge::router::CommandContext ctx{&f, 0, false, false, 0};

  // 1. Send all commands with empty payload to trigger MALFORMED / edge cases
  for (uint16_t cmd = 0; cmd < 255; cmd++) {
    f.header.command_id = cmd;
    f.payload = etl::span<const uint8_t>();
    ctx.raw_command = cmd;
    ba.dispatch(f);
  }

  // 2. Exhaustive valid payload generation using macro
#define COVER_CMD(CMD_NAME, STRUCT_NAME) do { \
  mcubridge_##STRUCT_NAME msg = mcubridge_##STRUCT_NAME##_init_default; \
  pb_ostream_t s = pb_ostream_from_buffer(payload_buffer.data(), payload_buffer.size()); \
  pb_encode(&s, mcubridge_##STRUCT_NAME##_fields, &msg); \
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_##CMD_NAME); \
  f.header.payload_length = s.bytes_written; \
  f.payload = etl::span<const uint8_t>(payload_buffer.data(), s.bytes_written); \
  ctx.raw_command = f.header.command_id; \
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

  // Specific path testing
  ba.handleGetVersion(ctx);
  ba.handleGetFreeMemory(ctx);

  // Flow Control
  Bridge.sendXoff();
  Bridge.sendXon();

  restore_bridge_to_serial();
}

static bool async_called = false;
static void dummy_async_handler(int16_t) { async_called = true; }

static bool poll_called = false;
static void dummy_poll_handler(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>) { poll_called = true; }

static void test_process_api() {
  BiStream stream;
  reset_bridge_with_stream(stream);
  
  async_called = false;
  poll_called = false;

  Process.runAsync("ls", {}, etl::delegate<void(int16_t)>::create<dummy_async_handler>());
  Process.poll(1, etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>::create<dummy_poll_handler>());
  
  Process.kill(1);
  
  // Simulate responses
  rpc::payload::ProcessRunAsyncResponse msg_run = {};
  msg_run.pid = 1;
  Process._onRunAsyncResponse(msg_run);
  
  rpc::payload::ProcessPollResponse msg_poll = {};
  msg_poll.status = rpc::to_underlying(rpc::StatusCode::STATUS_OK);
  msg_poll.exit_code = 0;
  Process._onPollResponse(msg_poll, etl::span<const uint8_t>(), etl::span<const uint8_t>());
  
  // Cover internal method
  Process._kill(1);
  
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
  
  Console._push(etl::span<const uint8_t>(buf, 2));
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
  DataStore._onResponse(etl::span<const uint8_t>(data, 3));
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
  Mailbox._onIncomingData(etl::span<const uint8_t>(data, 3));
  Mailbox._onResponse(etl::span<const uint8_t>(data, 3));
  rpc::payload::MailboxAvailableResponse msg = {};
  msg.count = 2;
  Mailbox._onAvailableResponse(msg);
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
