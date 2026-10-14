#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <unity.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"
#include <BridgeFaultInjection.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>
#include "hal/hal.h"
#include "test_support.h"

// Define the global delegates and stubs for HardwareSerial stub
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;

// Unity setup/teardown
void setUp(void) {}
void tearDown(void) {}

using namespace bridge::test;

void reset_bridge_comp(BiStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) bridge::test::TestAccessor(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "top-secret");
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
}

void test_all_handlers_coverage() {
  BiStream stream;
  reset_bridge_comp(stream);

  rpc_pb_RpcEnvelope frame = rpc_pb_RpcEnvelope_init_default;
  frame.version = rpc::PROTOCOL_VERSION;
  frame.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  TestAccessor::create(Bridge).dispatch(frame);
  Mailbox.process();

  frame.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
  TestAccessor::create(Bridge).dispatch(frame);
  Mailbox.process();

  frame.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
  TestAccessor::create(Bridge).dispatch(frame);
  Mailbox.process();

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
  TEST_ASSERT_FALSE(TestAccessor::create(Bridge).isAwaitingAck());
}

void test_process_api() {
  BiStream stream;
  reset_bridge_comp(stream);

#if BRIDGE_ENABLE_PROCESS
  Process.reset();
  TEST_ASSERT_TRUE(Process._pending_run_async.empty());
  TEST_ASSERT_TRUE(Process._pending_polls.empty());
#endif
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_console_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  Console.begin();
  TEST_ASSERT_EQUAL(1, Console.write('A'));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_datastore_api() {
  BiStream stream;
  reset_bridge_comp(stream);
#if BRIDGE_ENABLE_DATASTORE
  uint8_t dummy_val[] = {1, 2, 3};
  DataStore.set("test_k", etl::span<const uint8_t>(dummy_val, 3));
#endif
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

static bool message_callback_called = false;
static uint8_t last_message_data[64] = {0};
static size_t last_message_len = 0;
static void my_message_callback(etl::span<const uint8_t> data) {
  message_callback_called = true;
  last_message_len = etl::min(data.size(), sizeof(last_message_data));
  if (last_message_len > 0) {
    etl::copy_n(data.data(), last_message_len, last_message_data);
  }
}

static bool available_callback_called = false;
static uint32_t last_available_count = 0;
static void my_available_callback(uint32_t count) {
  available_callback_called = true;
  last_available_count = count;
}

void test_mailbox_api() {
  BiStream stream;
  reset_bridge_comp(stream);

#if BRIDGE_ENABLE_MAILBOX
  // 1. Register callbacks
  message_callback_called = false;
  available_callback_called = false;
  last_message_len = 0;
  last_available_count = 0;

  Mailbox.registerMessageCallback(
      MailboxType::MessageCallback::create<my_message_callback>());
  Mailbox.registerAvailableCallback(
      MailboxType::AvailableCallback::create<my_available_callback>());

  // 2. Test sending commands
  uint8_t raw_payload[] = {0xAA, 0xBB, 0xCC};
  Mailbox.push(etl::span<const uint8_t>(raw_payload, 3));
  TEST_ASSERT_TRUE(Mailbox.requestRead());
  TEST_ASSERT_TRUE(Mailbox.requestAvailable());
  Mailbox.signalProcessed(456);

  // 3. Test receiving events (dispatching)
  rpc_pb_RpcEnvelope frame = rpc_pb_RpcEnvelope_init_default;
  frame.version = rpc::PROTOCOL_VERSION;

  // CMD_MAILBOX_PUSH
  {
    rpc_pb_MailboxPush push_msg = {};
    push_msg.data.size = 2;
    push_msg.data.bytes[0] = 0x11;
    push_msg.data.bytes[1] = 0x22;
    frame.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH);
    bridge::test::set_pb_payload(frame, push_msg);
    TestAccessor::create(Bridge).dispatch(frame);
    Mailbox.process();
    TEST_ASSERT_TRUE(message_callback_called);
    TEST_ASSERT_EQUAL_UINT32(2, last_message_len);
    TEST_ASSERT_EQUAL_UINT8(0x11, last_message_data[0]);
    TEST_ASSERT_EQUAL_UINT8(0x22, last_message_data[1]);
  }

  // CMD_MAILBOX_READ_RESP
  {
    message_callback_called = false;
    rpc_pb_MailboxReadResponse read_resp = {};
    read_resp.content.size = 3;
    read_resp.content.bytes[0] = 0x33;
    read_resp.content.bytes[1] = 0x44;
    read_resp.content.bytes[2] = 0x55;
    frame.command_id =
        rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
    bridge::test::set_pb_payload(frame, read_resp);
    TestAccessor::create(Bridge).dispatch(frame);
    Mailbox.process();
    TEST_ASSERT_TRUE(message_callback_called);
    TEST_ASSERT_EQUAL_UINT32(3, last_message_len);
    TEST_ASSERT_EQUAL_UINT8(0x33, last_message_data[0]);
    TEST_ASSERT_EQUAL_UINT8(0x44, last_message_data[1]);
    TEST_ASSERT_EQUAL_UINT8(0x55, last_message_data[2]);
  }

  // CMD_MAILBOX_AVAILABLE_RESP
  {
    rpc_pb_MailboxAvailableResponse avail_resp = {};
    avail_resp.count = 42;
    frame.command_id =
        rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
    bridge::test::set_pb_payload(frame, avail_resp);
    TestAccessor::create(Bridge).dispatch(frame);
    Mailbox.process();
    TEST_ASSERT_TRUE(available_callback_called);
    TEST_ASSERT_EQUAL_UINT32(42, last_available_count);
  }
#endif
}

void test_send_pin_event_api() {
  BiStream stream;
  reset_bridge_comp(stream);

  const size_t initial_bytes = stream.tx_buf.len;
  const bool result = Bridge.sendPinEvent(13, 1023);
  TEST_ASSERT_TRUE(result);
  TEST_ASSERT_GREATER_THAN(initial_bytes, stream.tx_buf.len);

  Bridge.~BridgeClass();
  new (&Bridge) bridge::test::TestAccessor(stream);
  const bool unsync_result = Bridge.sendPinEvent(13, 1023);
  TEST_ASSERT_FALSE(unsync_result);
}

void test_spi_data_modes_map_to_native_settings() {
  SPIService.begin();
  etl::array<uint8_t, 1> payload = {0xA5};
  rpc::payload::SpiConfig config = {};
  config.frequency = 1000000;
  config.bit_order = rpc_pb_SpiBitOrder_SPI_BIT_ORDER_LSB_FIRST;

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_0;
  SPIService.setConfig(config);
  TEST_ASSERT_EQUAL_UINT32(1, SPIService.transfer(etl::span<uint8_t>(
                                  payload.data(), payload.size())));
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE0, SPI.last_settings.data_mode);

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_1;
  SPIService.setConfig(config);
  TEST_ASSERT_EQUAL_UINT32(1, SPIService.transfer(etl::span<uint8_t>(
                                  payload.data(), payload.size())));
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE1, SPI.last_settings.data_mode);

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_2;
  SPIService.setConfig(config);
  TEST_ASSERT_EQUAL_UINT32(1, SPIService.transfer(etl::span<uint8_t>(
                                  payload.data(), payload.size())));
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE2, SPI.last_settings.data_mode);

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_3;
  SPIService.setConfig(config);
  TEST_ASSERT_EQUAL_UINT32(1, SPIService.transfer(etl::span<uint8_t>(
                                  payload.data(), payload.size())));
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE3, SPI.last_settings.data_mode);
  TEST_ASSERT_EQUAL_UINT8(LSBFIRST, SPI.last_settings.bit_order);
  TEST_ASSERT_EQUAL_UINT32(config.frequency, SPI.last_settings.clock);

  config.data_mode = static_cast<rpc_pb_SpiDataMode>(99);
  SPIService.setConfig(config);
  TEST_ASSERT_EQUAL_UINT32(1, SPIService.transfer(etl::span<uint8_t>(
                                  payload.data(), payload.size())));
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE0, SPI.last_settings.data_mode);

  SPIService.end();
}

static bool g_rpc_mgr_cb_called = false;
static void test_rpc_mgr_callback(const rpc::payload::DatastoreGetResponse&) {
  g_rpc_mgr_cb_called = true;
}

static void dummy_fs_read(etl::span<const uint8_t>) {}
static void dummy_process_run(int32_t) {}
static void dummy_process_poll(rpc::StatusCode, uint16_t,
                               etl::span<const uint8_t>,
                               etl::span<const uint8_t>) {}

void test_component_extended_operations() {
  BiStream stream;
  reset_bridge_comp(stream);

  // Mailbox
  uint8_t mbox_data[16] = {1, 2, 3};
  Mailbox.push(etl::span<const uint8_t>(mbox_data, 3));
  Mailbox.onLost();
  TEST_ASSERT_TRUE(Mailbox.requestRead());
  TEST_ASSERT_TRUE(Mailbox.requestAvailable());
  Mailbox.signalProcessed(123);

  // FileSystem
  FileSystem.read("test.txt", etl::delegate<void(etl::span<const uint8_t>)>::create<dummy_fs_read>());
  FileSystem.write("test.txt", etl::span<const uint8_t>(mbox_data, 3));
  FileSystem.remove("test.txt");

  rpc::payload::FileReadResponse fr_p;
  rpc::payload::copy_to_pb_bytes(fr_p.content, mbox_data, 3);
  FileSystem._onResponse(fr_p);

  // Process
  Process.runAsync("ls", etl::span<const etl::string_view>(),
                   etl::delegate<void(int32_t)>::create<dummy_process_run>());
  Process.kill(1);
  Process.poll(1, etl::delegate<void(rpc::StatusCode, uint16_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>::create<dummy_process_poll>());

  rpc::payload::ProcessRunAsyncResponse prar;
  prar.pid = 123;
  Process._onRunAsyncResponse(prar);

  rpc::payload::ProcessPollResponse ppr_p;
  ppr_p.status = 0;
  ppr_p.exit_code = 0;
  Process._onPollResponse(ppr_p);

  // SPI uninitialized transfer
  SPIService.end();
  uint8_t spi_buf[2] = {0, 0};
  SPIService.transfer(etl::span<uint8_t>(spi_buf, 2));

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_pending_rpc_manager() {
  bridge::PendingRpcManager<rpc::payload::DatastoreGetResponse, 2> mgr;
  TEST_ASSERT_TRUE(mgr.empty());
  TEST_ASSERT_FALSE(mgr.full());
  TEST_ASSERT_EQUAL(0, mgr.size());
  TEST_ASSERT_EQUAL(2, mgr.capacity());
  TEST_ASSERT_EQUAL(2, mgr.available());

  auto h = bridge::PendingRpcManager<rpc::payload::DatastoreGetResponse, 2>::
      HandlerType::create<test_rpc_mgr_callback>();
  TEST_ASSERT_TRUE(mgr.push(h));
  TEST_ASSERT_EQUAL(1, mgr.size());
  TEST_ASSERT_TRUE(mgr.push(h));
  TEST_ASSERT_FALSE(mgr.push(h));
  TEST_ASSERT_TRUE(mgr.full());

  rpc::payload::DatastoreGetResponse resp = {};
  g_rpc_mgr_cb_called = false;
  TEST_ASSERT_TRUE(mgr.dispatch(
      resp, [](const auto& handler, const auto& msg) { handler(msg); }));
  TEST_ASSERT_TRUE(g_rpc_mgr_cb_called);
  TEST_ASSERT_EQUAL(1, mgr.size());
  mgr.pop();
  TEST_ASSERT_TRUE(mgr.empty());
  TEST_ASSERT_FALSE(mgr.dispatch(resp, [](const auto&, const auto&) {}));
  mgr.clear();
  TEST_ASSERT_TRUE(mgr.empty());
}

static int32_t s_captured_pid = 0;
static void capture_async_handler(int32_t pid) { s_captured_pid = pid; }
static void capture_poll_handler(rpc::StatusCode, uint16_t, etl::span<const uint8_t>, etl::span<const uint8_t>) {}

void test_process_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  // 1. Process poll and kill API
  Process.poll(123, ProcessType::ProcessPollHandler::create<&capture_poll_handler>());
  Process.kill(456);
  Process.runAsync("ls", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());

  // 2. Observer notification onLost
  Process.onLost();
  TEST_ASSERT_TRUE(Process._pending_run_async.empty());
  TEST_ASSERT_TRUE(Process._pending_polls.empty());

  // 3. Queue full and trigger full-queue error callback path (-1)
  s_captured_pid = 0;
  Process.runAsync("ls", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  Process.runAsync("pwd", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, s_captured_pid);
  TEST_ASSERT_EQUAL(1, Process._pending_run_async.size());

  rpc::payload::ProcessRunAsyncResponse p_resp;
  p_resp.pid = 42;
  Process._onRunAsyncResponse(p_resp);
  TEST_ASSERT_EQUAL(42, s_captured_pid);

  // 4. Invalid callback should not enqueue
  Process.reset();
  Process.runAsync("ls", {}, ProcessType::ProcessRunHandler{});
  TEST_ASSERT_EQUAL(0, Process._pending_run_async.size());

  // 5. Oversized arg failure
  etl::array<char, rpc::MAX_PAYLOAD_SIZE + 1> long_arg_storage = {};
  long_arg_storage.fill('a');
  const etl::string_view oversized_arg(long_arg_storage.data(), rpc::MAX_PAYLOAD_SIZE);
  etl::array<etl::string_view, 2> overflow_args = {oversized_arg, etl::string_view("y")};
  Process.runAsync("x", etl::span<const etl::string_view>(overflow_args.data(), 2),
                   etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, s_captured_pid);

  // 6. Prepend-space capacity failure
  etl::array<char, rpc::MAX_PAYLOAD_SIZE> near_full_cmd = {};
  near_full_cmd.fill('c');
  etl::array<etl::string_view, 1> single_arg = {etl::string_view("z")};
  Process.runAsync(etl::string_view(near_full_cmd.data(), rpc::MAX_PAYLOAD_SIZE - 1U),
                   etl::span<const etl::string_view>(single_arg.data(), 1),
                   etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, s_captured_pid);

  // 7. Empty command
  Process.runAsync("", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());

  // 8. Safe state with invalid handler
  Bridge.enterSafeState();
  ProcessType::ProcessRunHandler empty_run_handler;
  Process.runAsync("ls", {}, empty_run_handler);

  reset_bridge_comp(stream);

  // 9. Poll queue full path and empty response
  Process.reset();
  Process.poll(10, ProcessType::ProcessPollHandler::create<&capture_poll_handler>());
  TEST_ASSERT_EQUAL(1, Process._pending_polls.size());
  Process.poll(11, ProcessType::ProcessPollHandler::create<&capture_poll_handler>());

  Process._pending_run_async.clear();
  rpc_pb_ProcessRunAsyncResponse pr_empty = {};
  Process._onRunAsyncResponse(pr_empty);

  Process._pending_polls.clear();
  rpc_pb_ProcessPollResponse pp_empty = {};
  Process._onPollResponse(pp_empty);

  // 10. Process kill when send fails
  ba.setTxEnabled(false);
  Process.kill(999);
  ba.setTxEnabled(true);

  // 11. Oversized command string alone without args (cmd.size() > 64)
  char huge_cmd[80];
  etl::fill_n(huge_cmd, 79, 'q');
  huge_cmd[79] = '\0';
  Process.runAsync(huge_cmd, {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  Process.runAsync(huge_cmd, {}, ProcessType::ProcessRunHandler{});

  // 12. Non-empty args that fit into command_buffer (covers line 40 false branch)
  etl::string_view valid_args[1] = {etl::string_view("-la")};
  Process.runAsync("ls", etl::span<const etl::string_view>(valid_args, 1),
                   etl::delegate<void(int32_t)>::create<&capture_async_handler>());

  // 13. Send failure with valid and invalid handler (covers line 64)
  ba.setSynchronized();
  ba.setTxEnabled(false);
  Process.runAsync("ls", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  Process.runAsync("ls", {}, ProcessType::ProcessRunHandler{});
  ba.setTxEnabled(true);

  // Large cmd triggering pb_encode failure when encrypted
  const char* secret_str =
      "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe";
  ba.setSharedSecret(etl::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(secret_str), strlen(secret_str)));
  etl::array<char, 60> large_cmd_buf;
  large_cmd_buf.fill('x');
  large_cmd_buf[59] = '\0';
  Process.runAsync(
      etl::string_view(large_cmd_buf.data(), 59), {},
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  ba.clearSharedSecret();

  // 14. Poll with invalid handler
  Process.poll(999, ProcessType::ProcessPollHandler{});
}

void test_console_edges() {
  BiStream stream;
  reset_bridge_comp(stream);

  Console.begin();
  TEST_ASSERT_EQUAL(0U, Console.write(nullptr, 10));
  TEST_ASSERT_EQUAL(0U, Console.write((const uint8_t*)"abc", 0));
  TEST_ASSERT_EQUAL(-1, Console.read());
  TEST_ASSERT_EQUAL(-1, Console.peek());

  rpc::payload::ConsoleWrite cwrite = {};
  cwrite.data.size = 3;
  cwrite.data.bytes[0] = 'X';
  cwrite.data.bytes[1] = 'Y';
  cwrite.data.bytes[2] = 'Z';
  Console._push(cwrite);

  TEST_ASSERT_EQUAL((int)'X', Console.peek());
  TEST_ASSERT_EQUAL(3, Console.available());
  TEST_ASSERT_EQUAL((int)'X', Console.read());

  // Fill _tx_buffer completely then write with buffer when tx disabled to hit line 43
  auto& ba = TestAccessor::create(Bridge);
  etl::array<uint8_t, bridge::config::CONSOLE_TX_BUFFER_SIZE> fill = {};
  fill.fill('x');
  Console.write(fill.data(), fill.size());
  ba.setTxEnabled(false);
  const etl::array<uint8_t, 1> extra = {'y'};
  TEST_ASSERT_EQUAL_UINT32(0, Console.write(extra.data(), extra.size()));
  ba.setTxEnabled(true);

  // Multi-byte write exceeding available buffer to trigger to_write < size
  uint8_t large_console_buf[100];
  etl::fill_n(large_console_buf, 100, 'Z');
  Console.write(large_console_buf, 100);
  Console.write(large_console_buf, 50);

  // Full buffer write when send fails (retains data)
  ba.setTxEnabled(false);
  for (size_t i = 0; i < 70; ++i) {
    Console.write('A' + (i % 26));
  }
  ba.setTxEnabled(true);
  Console.onLost();
}

static void dummy_ds_edge_handler(etl::string_view, etl::span<const uint8_t>) {}
static bool s_ds_edge_called = false;
static void dummy_ds_valid_handler(etl::string_view, etl::span<const uint8_t>) {
  s_ds_edge_called = true;
}

void test_datastore_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Empty key/val
  DataStore.set(etl::string_view(), etl::span<const uint8_t>());
  rpc::payload::DatastoreGetResponse ds_resp = {};
  DataStore._onResponse(ds_resp);

  // Pending gets full
  auto dummy_handler = DataStoreClass::GetHandler::create<&dummy_ds_edge_handler>();
  etl::array<int, 10> get_steps{};
  etl::for_each(get_steps.begin(), get_steps.end(),
                [&](int) { DataStore.get("k", dummy_handler); });

  // Valid handler with response (covers lines 43, 44, 46)
  DataStore._pending_gets.clear();
  s_ds_edge_called = false;
  DataStore.get("k_valid", DataStoreClass::GetHandler::create<&dummy_ds_valid_handler>());
  rpc::payload::DatastoreGetResponse ds_resp_val = {};
  ds_resp_val.value.size = 2;
  ds_resp_val.value.bytes[0] = 0x55;
  ds_resp_val.value.bytes[1] = 0xAA;
  DataStore._onResponse(ds_resp_val);
  TEST_ASSERT_TRUE(s_ds_edge_called);

  // Invalid handler with synchronized bridge so handler gets pushed
  DataStore._pending_gets.clear();
  DataStoreClass::GetHandler invalid_handler;
  DataStore.get("k2", invalid_handler);
  DataStore._onResponse(ds_resp);

  // Empty queue response
  DataStore._pending_gets.clear();
  DataStore._onResponse(ds_resp);

  // Send fail branches
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  uint8_t payload_bytes[] = {'h', 'e', 'l', 'l', 'o'};
  DataStore.set("key2", etl::span<const uint8_t>(payload_bytes, 3));
  ba.setTxEnabled(true);

  DataStore.onLost();
}

static void dummy_fs_edge_handler(etl::span<const uint8_t>) {}

void test_filesystem_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  // 1. Unsynchronized / empty path send failure
  FileSystem.write(etl::string_view(), etl::span<const uint8_t>());
  FileSystem.read(etl::string_view(), FileSystemClass::FileSystemReadHandler());
  FileSystem.remove(etl::string_view());

  // 2. Read response with invalid handler
  FileSystem.read(etl::string_view(), FileSystemClass::FileSystemReadHandler());
  rpc::payload::FileReadResponse rresp = {};
  FileSystem._onResponse(rresp);

  // 3. Read response with valid handler
  FileSystem.read("test.txt",
      FileSystemClass::FileSystemReadHandler::create<&dummy_fs_edge_handler>());
  FileSystem._onResponse(rresp);

  // 4. Synchronized write, read, remove
  uint8_t data[] = {1, 2, 3};
  FileSystem.write("test.txt", etl::span<const uint8_t>(data, 3));
  FileSystem.read("test.txt",
      FileSystemClass::FileSystemReadHandler::create<&dummy_fs_edge_handler>());
  FileSystem.remove("test.txt");

  // 5. _onWrite and _onRemove when sendFrame fails
  ba.setTxEnabled(false);
  rpc_pb_FileWrite fw_msg = {};
  FileSystem._onWrite(fw_msg);
  rpc_pb_FileRemove fr_msg = {};
  FileSystem._onRemove(fr_msg);
  ba.setTxEnabled(true);

  // 6. _onRead non-existent file
  rpc_pb_FileRead fread_nonexistent = {};
  const char* non_path = "non_existent_file.xyz";
  etl::copy_n(non_path, strlen(non_path), fread_nonexistent.path);
  FileSystem._onRead(fread_nonexistent);

  // 7. _onRead timeout via fault injection
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);
  rpc_pb_FileRead fread_valid = {};
  const char* val_path = "test.txt";
  etl::copy_n(val_path, strlen(val_path), fread_valid.path);
  FileSystem._onRead(fread_valid);
  bridge::test::fault::disable(bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);

  // 8. Large file multi-chunk reading
  etl::array<uint8_t, 100> large_data;
  large_data.fill(0xAA);
  auto res_w1 = bridge::hal::writeFile("test_large.txt", large_data);
  TEST_ASSERT_TRUE(res_w1.has_value());
  rpc_pb_FileRead req_large = {};
  strncpy(req_large.path, "test_large.txt", sizeof(req_large.path));
  FileSystem._onRead(req_large);

  // 9. Send-fail branches when tx disabled
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  uint8_t fsdata[] = {1, 2};
  FileSystem.write("send_fail.txt", etl::span<const uint8_t>(fsdata, 2));
  FileSystem.remove("send_fail.txt");
  FileSystem._onRead(req_large);
  ba.setTxEnabled(true);

  // 10. Empty file (to_copy == 0)
  auto res_w2 = bridge::hal::writeFile("empty.txt", etl::span<const uint8_t>());
  TEST_ASSERT_TRUE(res_w2.has_value());
  rpc_pb_FileRead fread_empty = {};
  const char* empty_path = "empty.txt";
  etl::copy_n(empty_path, strlen(empty_path), fread_empty.path);
  FileSystem._onRead(fread_empty);

  // 11. Observer onLost
  FileSystem.onLost();
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_spi_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  // 1. SPI config
  SPIService.begin();
  rpc::payload::SpiConfig sc;
  sc.frequency = 4000000;
  sc.bit_order = rpc_pb_SpiBitOrder_SPI_BIT_ORDER_MSB_FIRST;
  sc.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_0;
  SPIService.setConfig(sc);

  // 2. Normal transfer, empty transfer, uninitialized transfer
  etl::array<uint8_t, 4> buf = {1, 2, 3, 4};
  size_t n = SPIService.transfer(etl::span<uint8_t>(buf));
  TEST_ASSERT_EQUAL(4, n);
  n = SPIService.transfer(etl::span<uint8_t>());
  TEST_ASSERT_EQUAL(0, n);

  SPIService.end();
  n = SPIService.transfer(etl::span<uint8_t>(buf));
  TEST_ASSERT_EQUAL(0, n);

  // 3. Timeout fault
  SPIService.begin();
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);
  size_t t3 = SPIService.transfer(etl::span<uint8_t>(buf));
  TEST_ASSERT_EQUAL(0U, t3);
  bridge::test::fault::disable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);

  // 4. Direct router handleSpiTransfer
  rpc_pb_SpiTransfer spi_req = rpc_pb_SpiTransfer_init_default;
  spi_req.data.size = 0;
  bridge::router::CommandContext spi_ctx(
      nullptr, static_cast<uint16_t>(rpc::CommandId::CMD_SPI_TRANSFER), 42,
      false, false);
  ba.handleSpiTransfer(spi_ctx, spi_req);
  ba.setTxEnabled(false);
  ba.handleSpiTransfer(spi_ctx, spi_req);
  ba.setTxEnabled(true);

  // 5. onLost
  SPIService.onLost();
}

static uint32_t s_mb_avail = 0;
static void mb_avail_cb(uint32_t c) { s_mb_avail = c; }
static bool s_mb_msg_called = false;
static void mb_msg_cb(etl::span<const uint8_t>) { s_mb_msg_called = true; }

void test_mailbox_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  // Enqueue via _onEnqueuePayload 5 times to overflow _queue (capacity 4)
  rpc_pb_MailboxPush mb_msg = rpc_pb_MailboxPush_init_default;
  mb_msg.data.size = 3;
  mb_msg.data.bytes[0] = 1;
  mb_msg.data.bytes[1] = 2;
  mb_msg.data.bytes[2] = 3;
  for (int i = 0; i < 5; ++i) {
    MailboxClass::_onEnqueuePayload<rpc_pb_MailboxPush, &rpc_pb_MailboxPush::data>(mb_msg);
  }

  // Available response with and without callback
  rpc::payload::MailboxAvailableResponse av_resp = {};
  av_resp.count = 2;
  Mailbox.registerAvailableCallback(MailboxClass::AvailableCallback::create<&mb_avail_cb>());
  Mailbox._onAvailableResponse(av_resp);
  TEST_ASSERT_EQUAL(2, s_mb_avail);
  Mailbox.registerAvailableCallback(MailboxClass::AvailableCallback{});
  Mailbox._onAvailableResponse(av_resp);

  // Process when queue has items but no message callback
  Mailbox.registerMessageCallback(MailboxClass::MessageCallback{});
  Mailbox.process();

  // Process with registered message callback
  s_mb_msg_called = false;
  Mailbox.registerMessageCallback(MailboxClass::MessageCallback::create<&mb_msg_cb>());
  Mailbox.process();
  TEST_ASSERT_TRUE(s_mb_msg_called);
  Mailbox.registerMessageCallback(MailboxClass::MessageCallback{});

  // Send fail
  uint8_t payload_bytes[] = {'h', 'e', 'l', 'l', 'o'};
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  Mailbox.push(etl::span<const uint8_t>(payload_bytes, 5));
  Mailbox.signalProcessed(789U);
  ba.setTxEnabled(true);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_all_handlers_coverage);
  RUN_TEST(test_process_api);
  RUN_TEST(test_console_api);
  RUN_TEST(test_datastore_api);
  RUN_TEST(test_mailbox_api);
  RUN_TEST(test_send_pin_event_api);
  RUN_TEST(test_spi_data_modes_map_to_native_settings);
  RUN_TEST(test_component_extended_operations);
  RUN_TEST(test_pending_rpc_manager);
  RUN_TEST(test_process_edges);
  RUN_TEST(test_console_edges);
  RUN_TEST(test_datastore_edges);
  RUN_TEST(test_filesystem_edges);
  RUN_TEST(test_spi_edges);
  RUN_TEST(test_mailbox_edges);
  return UNITY_END();
}