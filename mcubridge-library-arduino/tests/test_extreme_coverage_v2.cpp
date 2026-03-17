#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1
static unsigned long g_test_millis = 10000;  // Start at non-zero
unsigned long millis() { return g_test_millis++; }

#include "Bridge.h"

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "BridgeTestInterface.h"
#include "BridgeTestHelper.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "router/command_router.h"
#include "test_constants.h"
#include "test_support.h"

// Mocks y Stubs Globales
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
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

namespace {

void setup_env(BiStream& stream) {
  reset_bridge_core(Bridge, stream, 115200);
}

// --- COBERTURA BRIDGE.CPP ---
void test_bridge_gaps() {
  BiStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;

  // Gap: _handleSystemCommand default case
  f.header.command_id = 0x4F;
  ba.routeSystemCommand(bridge::router::CommandContext{&f, f.header.command_id, false, false});

  // Gap: _handleGpioCommand default case
  f.header.command_id = 0x5F;
  f.header.payload_length = 1;
  f.payload[0] = 13;
  ba.routeGpioCommand(bridge::router::CommandContext{&f, f.header.command_id, false, false});

  // Gap: dispatch unexpected status codes
  f.header.command_id = 0x3F;  // STATUS_CODE_MAX
  ba.dispatch(f);

  // Gap: dispatch with compressed flag but decode failure (short payload)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE) |
                        rpc::RPC_CMD_FLAG_COMPRESSED;
  f.header.payload_length = 1;
  f.payload[0] = 0xFF;  // RLE escape sin datos
  ba.dispatch(f);

  // Gap: _isRecentDuplicateRx branches
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.header.payload_length = 2;
  f.payload[0] = 13;
  f.payload[1] = 1;
  f.crc = 0x12345678;  // Dummy CRC
  ba.setAckTimeoutMs(1000);
  ba.setAckRetryLimit(3);
  ba.markRxProcessed(f);
  g_test_millis += 1500;
  assert(ba.isRecentDuplicateRx(f));

  // Gap: enterSafeState reset logic
  Bridge.enterSafeState();
  assert(!Bridge.isSynchronized());

  // Gap: _handleSystemCommand CMD_LINK_SYNC without secret
  ba.clearSharedSecret();
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  rpc::payload::LinkSync sync_msg = mcubridge_LinkSync_init_default;
  sync_msg.nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  etl::fill_n(sync_msg.nonce.bytes, rpc::RPC_HANDSHAKE_NONCE_LENGTH, uint8_t{0xA});
  bridge::test::set_pb_payload(f, sync_msg);
  ba.routeSystemCommand(bridge::router::CommandContext{&f, static_cast<uint16_t>(rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC)), false, false});

  // Gap: onPacketReceived with various errors
  uint8_t crc_err[] = {0x02, 0x00, 0x00, 0x40, 0x00, 0xDE, 0xAD, 0xBE, 0xEF};
  stream.feed(crc_err, sizeof(crc_err));
  Bridge.process();

  // Gap: Retransmission logic and failure streak
  ba.setUnsynchronized();
  ba.fsmHandshakeStart();
  ba.fsmHandshakeComplete();
  ba.fsmSendCritical();
  ba.setAckTimeoutMs(1000);
  ba.setAckRetryLimit(1);
  ba.setRetryCount(ba.getAckRetryLimit());

  ba.onAckTimeout();
  assert(!Bridge.isSynchronized());
}

// --- COBERTURA DATASTORE LÍMITES ---
void test_datastore_gaps() {
  BiStream stream;
  setup_env(stream);

  // Gap: _trackPendingDatastoreKey overflow
  for (int i = 0; i < static_cast<int>(bridge::config::MAX_PENDING_DATASTORE) + 1; ++i) {
    DataStore.get("key", DataStoreClass::DataStoreGetHandler{});
  }
}

// --- COBERTURA CONSOLE.CPP ---
void test_console_gaps() {
  BiStream stream;
  setup_env(stream);
  Console.begin();
  auto ca = bridge::test::ConsoleTestAccessor::create(Console);

  // Gap: write(buffer, size) chunking
  uint8_t large_buf[rpc::MAX_PAYLOAD_SIZE + 10];
  etl::fill_n(large_buf, sizeof(large_buf), uint8_t{'A'});
  Console.write(large_buf, sizeof(large_buf));

  // Gap: read() high/low watermarks
  for (int i = 0; i < static_cast<int>(bridge::config::CONSOLE_RX_BUFFER_SIZE); ++i)
    ca.pushRxByte(static_cast<uint8_t>(i));
  ca.setXoffSent(true);
  while (!ca.isRxBufferEmpty()) Console.read();
  assert(!ca.getXoffSent());

  // Gap: flush() with empty buffer
  ca.clearTxBuffer();
  Console.flush();
}

// --- COBERTURA FILESYSTEM.CPP ---
void test_filesystem_gaps() {
  BiStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Gap: write with data too large
  uint8_t super_large[rpc::MAX_PAYLOAD_SIZE + 10];
  FileSystem.write("test.txt", etl::span<const uint8_t>(super_large, sizeof(super_large)));

  // Gap: read() with invalid path
  FileSystem.read(etl::string_view{}, FileSystemClass::FileSystemReadHandler{});
  FileSystem.read("", FileSystemClass::FileSystemReadHandler{});
  char long_path[rpc::RPC_MAX_FILEPATH_LENGTH + 5];
  etl::fill_n(long_path, sizeof(long_path), 'p');
  long_path[sizeof(long_path) - 1] = '\0';
  FileSystem.read(long_path, FileSystemClass::FileSystemReadHandler{});

  // Gap: remove with overflowed path
  FileSystem.remove(long_path);

  // Gap: handleResponse with valid read handler
  FileSystem.read("testfile",
      FileSystemClass::FileSystemReadHandler::create([](etl::span<const uint8_t> d) {
        (void)d;
      }));
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
  rpc::payload::FileReadResponse fr_msg = mcubridge_FileReadResponse_init_default;
  uint8_t fr_data[] = "OK";
  rpc::util::pb_setup_encode_span(fr_msg.content, etl::span<const uint8_t>(fr_data, 2));
  bridge::test::set_pb_payload(f, fr_msg);
  ba.dispatch(f);
}

// --- COBERTURA MAILBOX.CPP ---
void test_mailbox_gaps() {
  BiStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Gap: requestRead, requestAvailable
  Mailbox.requestRead();
  Mailbox.requestAvailable();

  // Gap: handleResponse CMD_MAILBOX_AVAILABLE_RESP
  rpc::Frame f;
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  rpc::payload::MailboxAvailableResponse mb_msg = mcubridge_MailboxAvailableResponse_init_default;
  mb_msg.count = 5;
  bridge::test::set_pb_payload(f, mb_msg);
  ba.dispatch(f);

  // Gap: handleResponse with other command
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  ba.dispatch(f);
}

// --- COBERTURA PROCESS.CPP ---
void test_process_gaps() {
  BiStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Gap: poll with PID tracking
  {
    etl::string_view cmd{"test"};
    Process.runAsync(cmd, etl::span<const etl::string_view>{}, ProcessClass::ProcessRunAsyncHandler{});
  }
  // Simulamos que el Bridge recibió el PID 42
  rpc::Frame f_pid;
  f_pid.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  f_pid.header.payload_length = 2;
  rpc::write_u16_be(etl::span<uint8_t>(f_pid.payload.data(), 2), 42);
  ba.dispatch(f_pid);
  Process.poll(42, ProcessClass::ProcessPollHandler{});
  Process.kill(42);

  // Gap: handleResponse CMD_PROCESS_POLL_RESP (not running)
  rpc::Frame f;
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  rpc::payload::ProcessPollResponse poll_resp = mcubridge_ProcessPollResponse_init_default;
  poll_resp.status = static_cast<uint32_t>(rpc::StatusCode::STATUS_OK);
  poll_resp.exit_code = 0;
  
  uint8_t out_data[] = "o";
  uint8_t err_data[] = "e";
  rpc::util::pb_setup_encode_span(poll_resp.stdout_data, etl::span<const uint8_t>(out_data, 1));
  rpc::util::pb_setup_encode_span(poll_resp.stderr_data, etl::span<const uint8_t>(err_data, 1));
  
  bridge::test::set_pb_payload(f, poll_resp);
  ba.dispatch(f);
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_gaps);
  RUN_TEST(test_datastore_gaps);
  RUN_TEST(test_console_gaps);
  RUN_TEST(test_filesystem_gaps);
  RUN_TEST(test_mailbox_gaps);
  RUN_TEST(test_process_gaps);
  return UNITY_END();
}
Stream* g_arduino_stream_delegate = nullptr;
