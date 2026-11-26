#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <unity.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <BridgeFaultInjection.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <pw_span/span.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include <SPI.h>
#include "services/SPIService.h"
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
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, nullptr);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
}

void test_all_handlers_coverage() {
  BiStream stream;
  reset_bridge_comp(stream);

  auto& ba = TestAccessor::create(Bridge);
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("GetVersion"));
  Mailbox.process();
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("GetFreeMemory"));
  Mailbox.process();
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("GetCapabilities"));
  Mailbox.process();

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_process_api() {
  BiStream stream;
  reset_bridge_comp(stream);

#if BRIDGE_ENABLE_PROCESS
  Process.reset();
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
  DataStore.set("test_k", pw::span<const uint8_t>(dummy_val, 3));
#endif
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

static bool message_callback_called = false;
static uint8_t last_message_data[64] = {0};
static size_t last_message_len = 0;
static void my_message_callback(pw::span<const uint8_t> data) {
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
  Mailbox.push(pw::span<const uint8_t>(raw_payload, 3));
  TEST_ASSERT_TRUE(Mailbox.requestRead());
  TEST_ASSERT_TRUE(Mailbox.requestAvailable());
  Mailbox.signalProcessed(456);

  // 3. Test receiving events (dispatching)
  // bridge.MailboxService/Push (request from MPU)
  {
    rpc_pb_MailboxPush push_msg = rpc_pb_MailboxPush_init_default;
    push_msg.data.size = 2;
    push_msg.data.bytes[0] = 0x11;
    push_msg.data.bytes[1] = 0x22;
    send_rpc_request(TestAccessor::create(Bridge),
                     pw::tokenizer::Hash("rpc.pb.MailboxService"),
                     pw::tokenizer::Hash("Push"), rpc_pb_MailboxPush_fields,
                     push_msg);
    Mailbox.process();
    TEST_ASSERT_TRUE(message_callback_called);
    TEST_ASSERT_EQUAL_UINT32(2, last_message_len);
    TEST_ASSERT_EQUAL_UINT8(0x11, last_message_data[0]);
    TEST_ASSERT_EQUAL_UINT8(0x22, last_message_data[1]);
  }

  // bridge.MailboxService/Read (response to MCU requestRead)
  {
    message_callback_called = false;
    rpc_pb_MailboxReadResponse read_resp =
        rpc_pb_MailboxReadResponse_init_default;
    read_resp.content.size = 3;
    read_resp.content.bytes[0] = 0x33;
    read_resp.content.bytes[1] = 0x44;
    read_resp.content.bytes[2] = 0x55;
    invoke_rpc_response(TestAccessor::create(Bridge),
                        pw::tokenizer::Hash("rpc.pb.MailboxService"),
                        pw::tokenizer::Hash("Read"),
                        rpc_pb_MailboxReadResponse_fields, read_resp);
    Mailbox.process();
    TEST_ASSERT_TRUE(message_callback_called);
    TEST_ASSERT_EQUAL_UINT32(3, last_message_len);
    TEST_ASSERT_EQUAL_UINT8(0x33, last_message_data[0]);
    TEST_ASSERT_EQUAL_UINT8(0x44, last_message_data[1]);
    TEST_ASSERT_EQUAL_UINT8(0x55, last_message_data[2]);
  }

  // bridge.MailboxService/Available (response to MCU requestAvailable)
  {
    rpc_pb_MailboxAvailableResponse avail_resp =
        rpc_pb_MailboxAvailableResponse_init_default;
    avail_resp.count = 42;
    invoke_rpc_response(TestAccessor::create(Bridge),
                        pw::tokenizer::Hash("rpc.pb.MailboxService"),
                        pw::tokenizer::Hash("Available"),
                        rpc_pb_MailboxAvailableResponse_fields, avail_resp);
    Mailbox.process();
    TEST_ASSERT_TRUE(available_callback_called);
    TEST_ASSERT_EQUAL_UINT32(42, last_available_count);
  }
#endif
}

void test_send_pin_event_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  TEST_ASSERT_FALSE(Bridge.sendPinEvent(13, 1023));

  rpc_pb_PinSubscribeRequest sub_req = rpc_pb_PinSubscribeRequest_init_default;
  sub_req.pin = 13;
  sub_req.enabled = true;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("SubscribePins"),
                   rpc_pb_PinSubscribeRequest_fields, sub_req);

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
  rpc_pb_SpiConfig config = rpc_pb_SpiConfig_init_default;
  config.frequency = 1000000;
  config.bit_order = rpc_pb_SpiBitOrder_SPI_BIT_ORDER_LSB_FIRST;

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_0;
  TEST_ASSERT_TRUE(SPIService.setConfig(config).ok());
  TEST_ASSERT_TRUE(SPIService.transfer(pw::span<uint8_t>(
                                   payload.data(), payload.size())).ok());
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE0, SPI.last_settings.data_mode);

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_1;
  TEST_ASSERT_TRUE(SPIService.setConfig(config).ok());
  TEST_ASSERT_TRUE(SPIService.transfer(pw::span<uint8_t>(
                                   payload.data(), payload.size())).ok());
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE1, SPI.last_settings.data_mode);

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_2;
  TEST_ASSERT_TRUE(SPIService.setConfig(config).ok());
  TEST_ASSERT_TRUE(SPIService.transfer(pw::span<uint8_t>(
                                   payload.data(), payload.size())).ok());
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE2, SPI.last_settings.data_mode);

  config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_3;
  TEST_ASSERT_TRUE(SPIService.setConfig(config).ok());
  TEST_ASSERT_TRUE(SPIService.transfer(pw::span<uint8_t>(
                                   payload.data(), payload.size())).ok());
  TEST_ASSERT_EQUAL_UINT8(SPI_MODE3, SPI.last_settings.data_mode);
  TEST_ASSERT_EQUAL_UINT8(LSBFIRST, SPI.last_settings.bit_order);
  TEST_ASSERT_EQUAL_UINT32(config.frequency, SPI.last_settings.clock);

  config.data_mode = static_cast<rpc_pb_SpiDataMode>(99);
  TEST_ASSERT_EQUAL(pw::Status::InvalidArgument(),
                    SPIService.setConfig(config));

  SPIService.end();
}

static void dummy_process_run(int32_t) {}
static void dummy_process_poll(pw::Status, uint16_t, pw::span<const uint8_t>,
                               pw::span<const uint8_t>) {}

void test_component_extended_operations() {
  BiStream stream;
  reset_bridge_comp(stream);

  // Mailbox
  uint8_t mbox_data[16] = {1, 2, 3};
  Mailbox.push(pw::span<const uint8_t>(mbox_data, 3));
  Mailbox.onLost();
  TEST_ASSERT_TRUE(Mailbox.requestRead());
  TEST_ASSERT_TRUE(Mailbox.requestAvailable());
  Mailbox.signalProcessed(123);

  // FileSystem
  TEST_ASSERT_TRUE(
      FileSystem.upload("test.txt", pw::span<const uint8_t>(mbox_data, 3)));
  TEST_ASSERT_TRUE(FileSystem.uploadPending());
  FileSystem._resetUpload();
  TEST_ASSERT_FALSE(FileSystem.uploadPending());
  FileSystem.remove("test.txt");

  // Process
  Process.runAsync("ls", pw::span<const etl::string_view>(),
                   etl::delegate<void(int32_t)>::create<dummy_process_run>());
  Process.kill(1);
  Process.poll(1, etl::delegate<void(
                      pw::Status, uint16_t, pw::span<const uint8_t>,
                      pw::span<const uint8_t>)>::create<dummy_process_poll>());

  rpc_pb_ProcessRunAsyncResponse prar =
      rpc_pb_ProcessRunAsyncResponse_init_default;
  prar.pid = 123;
  Process._onRunAsyncResponse(prar);

  rpc_pb_ProcessPollResponse ppr_p = rpc_pb_ProcessPollResponse_init_default;
  ppr_p.status = 0;
  ppr_p.exit_code = 0;
  Process._onPollResponse(ppr_p);

  // SPI uninitialized transfer
  SPIService.end();
  uint8_t spi_buf[2] = {0, 0};
  SPIService.transfer(pw::span<uint8_t>(spi_buf, 2));

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

static int32_t s_captured_pid = 0;
static void capture_async_handler(int32_t pid) { s_captured_pid = pid; }
static void capture_poll_handler(pw::Status, uint16_t, pw::span<const uint8_t>,
                                 pw::span<const uint8_t>) {}

void test_process_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  // 1. Process poll and kill API
  Process.poll(
      123, ProcessType::ProcessPollHandler::create<&capture_poll_handler>());
  Process.kill(456);
  Process.runAsync(
      "ls", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());

  // 2. Observer notification onLost
  Process.onLost();

  // 3. Queue full and trigger full-queue error callback path (-1)
  s_captured_pid = 0;
  Process.runAsync(
      "ls", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  Process.runAsync(
      "pwd", {},
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, s_captured_pid);

  rpc_pb_ProcessRunAsyncResponse p_resp =
      rpc_pb_ProcessRunAsyncResponse_init_default;
  p_resp.pid = 42;
  Process._onRunAsyncResponse(p_resp);
  TEST_ASSERT_EQUAL(42, s_captured_pid);

  // 4. Invalid callback should not enqueue, then reset before arg-validation
  // tests
  Process.reset();
  Process.runAsync("ls", {}, ProcessType::ProcessRunHandler{});
  Process.reset();

  // 5. Oversized arg failure
  etl::array<char, rpc::MAX_PAYLOAD_SIZE + 1> long_arg_storage = {};
  long_arg_storage.fill('a');
  const etl::string_view oversized_arg(long_arg_storage.data(),
                                       rpc::MAX_PAYLOAD_SIZE);
  etl::array<etl::string_view, 2> overflow_args = {oversized_arg,
                                                   etl::string_view("y")};
  Process.runAsync(
      "x", pw::span<const etl::string_view>(overflow_args.data(), 2),
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, s_captured_pid);

  // 6. Prepend-space capacity failure
  etl::array<char, rpc::MAX_PAYLOAD_SIZE> near_full_cmd = {};
  near_full_cmd.fill('c');
  etl::array<etl::string_view, 1> single_arg = {etl::string_view("z")};
  Process.runAsync(
      etl::string_view(near_full_cmd.data(), rpc::MAX_PAYLOAD_SIZE - 1U),
      pw::span<const etl::string_view>(single_arg.data(), 1),
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, s_captured_pid);

  // 7. Empty command
  Process.runAsync(
      "", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());

  // 8. Safe state with invalid handler
  Bridge.enterSafeState();
  ProcessType::ProcessRunHandler empty_run_handler;
  Process.runAsync("ls", {}, empty_run_handler);

  reset_bridge_comp(stream);

  // 9. Poll queue full path and empty response
  Process.reset();
  Process.poll(
      10, ProcessType::ProcessPollHandler::create<&capture_poll_handler>());
  Process.poll(
      11, ProcessType::ProcessPollHandler::create<&capture_poll_handler>());

  Process.reset();
  rpc_pb_ProcessRunAsyncResponse pr_empty =
      rpc_pb_ProcessRunAsyncResponse_init_default;
  Process._onRunAsyncResponse(pr_empty);

  rpc_pb_ProcessPollResponse pp_empty = rpc_pb_ProcessPollResponse_init_default;
  Process._onPollResponse(pp_empty);

  // 10. Process kill when send fails
  ba.setTxEnabled(false);
  Process.kill(999);
  ba.setTxEnabled(true);

  // 11. Oversized command string alone without args (cmd.size() > 64)
  char huge_cmd[80];
  etl::fill_n(huge_cmd, 79, 'q');
  huge_cmd[79] = '\0';
  Process.runAsync(
      huge_cmd, {},
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  Process.runAsync(huge_cmd, {}, ProcessType::ProcessRunHandler{});

  // 12. Non-empty args that fit into command_buffer (covers line 40 false
  // branch)
  etl::string_view valid_args[1] = {etl::string_view("-la")};
  Process.runAsync(
      "ls", pw::span<const etl::string_view>(valid_args, 1),
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());

  // 13. Send failure with valid and invalid handler (covers line 64)
  Process.reset();
  ba.setSynchronized();
  ba.setTxEnabled(false);
  Process.runAsync(
      "ls", {}, etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  Process.runAsync("ls", {}, ProcessType::ProcessRunHandler{});
  ba.setTxEnabled(true);

  // Unready session keys and ready session keys branches
  Process.reset();
  const char* secret_str =
      "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe";
  ba.setSharedSecret(pw::span<const uint8_t>(
      reinterpret_cast<const uint8_t*>(secret_str), strlen(secret_str)));
  etl::array<char, 60> large_cmd_buf;
  large_cmd_buf.fill('x');
  large_cmd_buf[59] = '\0';
  Process.runAsync(
      etl::string_view(large_cmd_buf.data(), 59), {},
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  ba.setSessionKeysReady(true);
  Process.kill(1);
  ba.setSessionKeysReady(false);
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

  uint8_t cdata[] = {'X', 'Y', 'Z'};
  Console.pushBytes(cdata, sizeof(cdata));

  TEST_ASSERT_EQUAL((int)'X', Console.peek());
  TEST_ASSERT_EQUAL(3, Console.available());
  TEST_ASSERT_EQUAL((int)'X', Console.read());

  // Fill _tx_buffer completely then write with buffer when tx disabled to hit
  // line 43
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
  etl::array<uint8_t, 70> console_chars{};
  size_t char_idx = 0;
  etl::generate(console_chars.begin(), console_chars.end(), [&char_idx]() {
    return static_cast<uint8_t>('A' + (char_idx++ % 26));
  });
  etl::for_each(console_chars.begin(), console_chars.end(),
                [](uint8_t c) { Console.write(c); });
  ba.setTxEnabled(true);
  Console.onLost();
}

static void dummy_ds_edge_handler(etl::string_view, pw::span<const uint8_t>) {}
static bool s_ds_edge_called = false;
static void dummy_ds_valid_handler(etl::string_view, pw::span<const uint8_t>) {
  s_ds_edge_called = true;
}

void test_datastore_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Empty key/val
  DataStore.set(etl::string_view(), pw::span<const uint8_t>());
  rpc_pb_DatastoreGetResponse ds_resp =
      rpc_pb_DatastoreGetResponse_init_default;
  DataStore._onResponse(ds_resp);

  // Pending gets full
  auto dummy_handler =
      DataStoreClass::GetHandler::create<&dummy_ds_edge_handler>();
  etl::array<int, 10> get_steps{};
  etl::for_each(get_steps.begin(), get_steps.end(),
                [&](int) { DataStore.get("k", dummy_handler); });

  // Valid handler with response
  DataStore.onLost();
  s_ds_edge_called = false;
  DataStore.get("k_valid",
                DataStoreClass::GetHandler::create<&dummy_ds_valid_handler>());
  rpc_pb_DatastoreGetResponse ds_resp_val =
      rpc_pb_DatastoreGetResponse_init_default;
  ds_resp_val.value.size = 2;
  ds_resp_val.value.bytes[0] = 0x55;
  ds_resp_val.value.bytes[1] = 0xAA;
  DataStore._onResponse(ds_resp_val);
  TEST_ASSERT_TRUE(s_ds_edge_called);

  // Invalid handler with synchronized bridge so handler gets pushed
  DataStore.onLost();
  DataStoreClass::GetHandler invalid_handler;
  DataStore.get("k2", invalid_handler);
  DataStore._onResponse(ds_resp);

  // Empty queue response
  DataStore.onLost();
  DataStore._onResponse(ds_resp);

  // Send fail branches
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  uint8_t payload_bytes[] = {'h', 'e', 'l', 'l', 'o'};
  DataStore.set("key2", pw::span<const uint8_t>(payload_bytes, 3));
  ba.setTxEnabled(true);

  DataStore.onLost();
}

void test_filesystem_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  TEST_ASSERT_FALSE(
      FileSystem.upload(etl::string_view(), pw::span<const uint8_t>()));
  FileSystem.remove(etl::string_view());

  ba.setTxEnabled(false);
  const uint8_t payload[] = {0x01U, 0x02U};
  TEST_ASSERT_FALSE(FileSystem.upload("offline.bin", payload));
  rpc_pb_FileRemove remove_request = {};
  FileSystem._onRemove(remove_request);
  ba.setTxEnabled(true);

  Bridge.enterSafeState();
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_spi_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  // 1. SPI config
  SPIService.begin();
  rpc_pb_SpiConfig sc = rpc_pb_SpiConfig_init_default;
  sc.frequency = 4000000;
  sc.bit_order = rpc_pb_SpiBitOrder_SPI_BIT_ORDER_MSB_FIRST;
  sc.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_0;
  TEST_ASSERT_TRUE(SPIService.setConfig(sc).ok());

  // 2. Normal transfer, empty transfer, uninitialized transfer
  etl::array<uint8_t, 4> buf = {1, 2, 3, 4};
  TEST_ASSERT_TRUE(SPIService.transfer(pw::span<uint8_t>(buf)).ok());
  TEST_ASSERT_TRUE(SPIService.transfer(pw::span<uint8_t>()).ok());

  SPIService.end();
  TEST_ASSERT_EQUAL(pw::Status::FailedPrecondition(),
                    SPIService.transfer(pw::span<uint8_t>(buf)));

  // 3. Timeout fault
  SPIService.begin();
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);
  TEST_ASSERT_EQUAL(pw::Status::DeadlineExceeded(),
                    SPIService.transfer(pw::span<uint8_t>(buf)));
  bridge::test::fault::disable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);

  // 4. SPI Transfer via HDLC RPC
  rpc_pb_SpiTransfer spi_req = rpc_pb_SpiTransfer_init_default;
  spi_req.data.size = 0;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SpiService"),
                   pw::tokenizer::Hash("Transfer"), rpc_pb_SpiTransfer_fields,
                   spi_req);
  ba.setTxEnabled(false);
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SpiService"),
                   pw::tokenizer::Hash("Transfer"), rpc_pb_SpiTransfer_fields,
                   spi_req);
  ba.setTxEnabled(true);

  // 5. onLost
  SPIService.onLost();
}

static uint32_t s_mb_avail = 0;
static void mb_avail_cb(uint32_t c) { s_mb_avail = c; }
static bool s_mb_msg_called = false;
static void mb_msg_cb(pw::span<const uint8_t>) { s_mb_msg_called = true; }

void test_mailbox_edges() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);

  // Enqueue via _enqueue 5 times to overflow _queue (capacity 4)
  rpc_pb_MailboxPush mb_msg = rpc_pb_MailboxPush_init_default;
  mb_msg.data.size = 3;
  mb_msg.data.bytes[0] = 1;
  mb_msg.data.bytes[1] = 2;
  mb_msg.data.bytes[2] = 3;
  constexpr etl::array<uint8_t, 5> push_attempts{};
  etl::for_each(push_attempts.begin(), push_attempts.end(), [&mb_msg](uint8_t) {
    MailboxClass::_enqueue(
        pw::span<const uint8_t>(mb_msg.data.bytes, mb_msg.data.size));
  });

  // Available response with and without callback
  rpc_pb_MailboxAvailableResponse av_resp =
      rpc_pb_MailboxAvailableResponse_init_default;
  av_resp.count = 2;
  Mailbox.registerAvailableCallback(
      MailboxClass::AvailableCallback::create<&mb_avail_cb>());
  Mailbox._onAvailableResponse(av_resp);
  TEST_ASSERT_EQUAL(2, s_mb_avail);
  Mailbox.registerAvailableCallback(MailboxClass::AvailableCallback{});
  Mailbox._onAvailableResponse(av_resp);

  // Process when queue has items but no message callback
  Mailbox.registerMessageCallback(MailboxClass::MessageCallback{});
  Mailbox.process();

  // Process with registered message callback
  s_mb_msg_called = false;
  Mailbox.registerMessageCallback(
      MailboxClass::MessageCallback::create<&mb_msg_cb>());
  Mailbox.process();
  TEST_ASSERT_TRUE(s_mb_msg_called);
  Mailbox.registerMessageCallback(MailboxClass::MessageCallback{});

  // Send fail
  uint8_t payload_bytes[] = {'h', 'e', 'l', 'l', 'o'};
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  Mailbox.push(pw::span<const uint8_t>(payload_bytes, 5));
  TEST_ASSERT_FALSE(Mailbox.requestRead());
  TEST_ASSERT_FALSE(Mailbox.requestAvailable());
  Mailbox.signalProcessed(789U);
  ba.setTxEnabled(true);

  // Exercise _serialTask() full HDLC frame decode and kResponse / kServerStream
  // / kClientError
  etl::array<uint8_t, 64> hdlc_buf;
  size_t sz = build_hdlc_rpc_frame(
      1, pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.SystemService"),
      pw::tokenizer::Hash("GetVersion"), pw::span<const uint8_t>(), hdlc_buf);
  stream.feed(hdlc_buf.data(), sz);
  Bridge.process();

  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.MailboxService"),
                pw::tokenizer::Hash("Available"), {},
                pw::rpc::internal::pwpb::PacketType::RESPONSE);
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.MailboxService"),
                pw::tokenizer::Hash("Read"), {},
                pw::rpc::internal::pwpb::PacketType::SERVER_STREAM);
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.MailboxService"),
                pw::tokenizer::Hash("Read"), {},
                pw::rpc::internal::pwpb::PacketType::CLIENT_ERROR);

  // Unknown method_id fallthroughs in RPC services and _dispatchRpcResponse
  constexpr uint32_t kUnknownMethod = 0xDEADBEEFU;
  auto send_rpc = [&](pw::rpc::internal::pwpb::PacketType ptype, uint32_t srv,
                      uint32_t meth, pw::span<const uint8_t> pl = {}) {
    send_hdlc_rpc(ba, srv, meth, pl, ptype);
  };
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.GpioService"), kUnknownMethod);
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.ClockSyncService"), kUnknownMethod);
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.SpiService"), kUnknownMethod);
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.FileSystemService"), kUnknownMethod);
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.MailboxService"), kUnknownMethod);
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Read"));
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Available"));
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Processed"));

  send_rpc(pw::rpc::internal::pwpb::PacketType::RESPONSE,
           pw::tokenizer::Hash("rpc.pb.MailboxService"), kUnknownMethod);
  send_rpc(pw::rpc::internal::pwpb::PacketType::RESPONSE,
           pw::tokenizer::Hash("rpc.pb.ProcessService"), kUnknownMethod);
  send_rpc(pw::rpc::internal::pwpb::PacketType::RESPONSE,
           pw::tokenizer::Hash("rpc.pb.FileSystemService"), kUnknownMethod);

  // Malformed protobuf payloads for SystemService::LinkSync and
  // MailboxService::Push
  const uint8_t bad_pb[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                            0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.SystemService"),
           pw::tokenizer::Hash("LinkSync"),
           pw::span<const uint8_t>(bad_pb, sizeof(bad_pb)));
  send_rpc(pw::rpc::internal::pwpb::PacketType::REQUEST,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Push"),
           pw::span<const uint8_t>(bad_pb, sizeof(bad_pb)));

  Console.pushBytes(nullptr, 5);
  Console.pushBytes(payload_bytes, 0);
}

namespace {

pw::Status s_rpc_last_status = pw::OkStatus();
size_t s_rpc_status_calls = 0U;
void capture_rpc_status(pw::Status s, pw::span<const uint8_t>) {
  s_rpc_last_status = s;
  ++s_rpc_status_calls;
}

int32_t s_r22_run_pid_1 = 0;
size_t s_r22_run_calls_1 = 0U;
void r22_run_handler_1(int32_t pid) {
  s_r22_run_pid_1 = pid;
  ++s_r22_run_calls_1;
}

int32_t s_r22_run_pid_2 = 0;
size_t s_r22_run_calls_2 = 0U;
void r22_run_handler_2(int32_t pid) {
  s_r22_run_pid_2 = pid;
  ++s_r22_run_calls_2;
}

pw::Status s_r22_poll_status_1 = pw::OkStatus();
uint16_t s_r22_poll_exit_1 = 0U;
size_t s_r22_poll_calls_1 = 0U;
void r22_poll_handler_1(pw::Status status, uint16_t exit_code,
                        pw::span<const uint8_t>, pw::span<const uint8_t>) {
  s_r22_poll_status_1 = status;
  s_r22_poll_exit_1 = exit_code;
  ++s_r22_poll_calls_1;
}

pw::Status s_r22_poll_status_2 = pw::OkStatus();
size_t s_r22_poll_calls_2 = 0U;
void r22_poll_handler_2(pw::Status status, uint16_t, pw::span<const uint8_t>,
                        pw::span<const uint8_t>) {
  s_r22_poll_status_2 = status;
  ++s_r22_poll_calls_2;
}

size_t s_r22_ds_calls_1 = 0U;
uint8_t s_r22_ds_first_byte_1 = 0U;
void r22_ds_handler_1(etl::string_view, pw::span<const uint8_t> val) {
  ++s_r22_ds_calls_1;
  s_r22_ds_first_byte_1 = val.empty() ? 0U : val[0];
}

size_t s_r22_ds_calls_2 = 0U;
void r22_ds_handler_2(etl::string_view, pw::span<const uint8_t>) {
  ++s_r22_ds_calls_2;
}

template <typename NanopbType>
void invoke_rpc_response_with_status(TestAccessor& ba, uint32_t service_id,
                                     uint32_t method_id,
                                     const pb_msgdesc_t* fields,
                                     const NanopbType& msg, pw::Status status) {
  uint8_t payload_buf[128];
  pb_ostream_t pbos = pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
  TEST_ASSERT_TRUE(pb_encode(&pbos, fields, &msg));
  pw::rpc::internal::Packet pkt(
      pw::rpc::internal::pwpb::PacketType::RESPONSE, 1U, service_id, method_id,
      0U, pw::as_bytes(pw::span(payload_buf, pbos.bytes_written)), status);
  ba.invokeRpcResponse(pkt);
}

void invoke_rpc_server_error(TestAccessor& ba, uint32_t service_id,
                             uint32_t method_id, pw::Status status) {
  pw::rpc::internal::Packet pkt(
      pw::rpc::internal::pwpb::PacketType::SERVER_ERROR, 1U, service_id,
      method_id, 0U, pw::ConstByteSpan(), status);
  ba.invokeRpcResponse(pkt);
}

}  // namespace

// Regression (R22): Process RPC client must enforce single in-flight call
// correlation without clobbering active receivers, and propagate non-OK
// RESPONSE status and SERVER_ERROR cleanly.
void test_process_rpc_correlation_and_status_propagation() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);
  constexpr uint32_t kProcSvc = pw::tokenizer::Hash("rpc.pb.ProcessService");
  constexpr uint32_t kRunMeth = pw::tokenizer::Hash("RunAsync");
  constexpr uint32_t kPollMeth = pw::tokenizer::Hash("Poll");

  s_rpc_status_calls = 0U;
  s_rpc_last_status = pw::OkStatus();
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_rpc_status>());
  Process.reset();

  // 1. Overlapping runAsync (valid handler and invalid handler) must NOT
  // clobber the active call; original handler still gets its response.
  s_r22_run_calls_1 = 0U;
  s_r22_run_pid_1 = 0;
  s_r22_run_calls_2 = 0U;
  s_r22_run_pid_2 = 0;
  Process.runAsync("first", {},
                   ProcessType::ProcessRunHandler::create<r22_run_handler_1>());
  Process.runAsync("second", {},
                   ProcessType::ProcessRunHandler::create<r22_run_handler_2>());
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_run_calls_2);
  TEST_ASSERT_EQUAL_INT32(-1, s_r22_run_pid_2);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::ResourceExhausted().code()),
      static_cast<int>(s_rpc_last_status.code()));

  // Overlapping with invalid handler also rejects cleanly without clobbering.
  Process.runAsync("third", {}, ProcessType::ProcessRunHandler{});
  TEST_ASSERT_EQUAL_UINT32(0U, s_r22_run_calls_1);

  rpc_pb_ProcessRunAsyncResponse run_resp =
      rpc_pb_ProcessRunAsyncResponse_init_default;
  run_resp.pid = 77U;
  invoke_rpc_response_with_status(ba, kProcSvc, kRunMeth,
                                  rpc_pb_ProcessRunAsyncResponse_fields,
                                  run_resp, pw::OkStatus());
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_run_calls_1);
  TEST_ASSERT_EQUAL_INT32(77, s_r22_run_pid_1);

  // 2. Non-OK RESPONSE status emits status, returns -1, and frees the slot.
  s_rpc_status_calls = 0U;
  s_r22_run_calls_1 = 0U;
  s_r22_run_pid_1 = 0;
  Process.runAsync("denied", {},
                   ProcessType::ProcessRunHandler::create<r22_run_handler_1>());
  rpc_pb_ProcessRunAsyncResponse err_resp =
      rpc_pb_ProcessRunAsyncResponse_init_default;
  err_resp.pid = 0U;
  invoke_rpc_response_with_status(ba, kProcSvc, kRunMeth,
                                  rpc_pb_ProcessRunAsyncResponse_fields,
                                  err_resp, pw::Status::PermissionDenied());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::PermissionDenied().code()),
                        static_cast<int>(s_rpc_last_status.code()));
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_run_calls_1);
  TEST_ASSERT_EQUAL_INT32(-1, s_r22_run_pid_1);

  // 3. SERVER_ERROR invokes on_error, emits status, passes -1, frees slot.
  s_rpc_status_calls = 0U;
  s_r22_run_calls_1 = 0U;
  Process.runAsync("server_err", {},
                   ProcessType::ProcessRunHandler::create<r22_run_handler_1>());
  invoke_rpc_server_error(ba, kProcSvc, kRunMeth, pw::Status::Unavailable());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Unavailable().code()),
                        static_cast<int>(s_rpc_last_status.code()));
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_run_calls_1);
  TEST_ASSERT_EQUAL_INT32(-1, s_r22_run_pid_1);

  // Also cover on_error with an invalid handler.
  Process.runAsync("no_cb", {}, ProcessType::ProcessRunHandler{});
  invoke_rpc_server_error(ba, kProcSvc, kRunMeth, pw::Status::Internal());

  // 4. Overlapping poll must NOT clobber active poll; original still completes.
  s_r22_poll_calls_1 = 0U;
  s_r22_poll_calls_2 = 0U;
  Process.poll(77,
               ProcessType::ProcessPollHandler::create<r22_poll_handler_1>());
  Process.poll(78,
               ProcessType::ProcessPollHandler::create<r22_poll_handler_2>());
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_poll_calls_2);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::ResourceExhausted().code()),
      static_cast<int>(s_r22_poll_status_2.code()));
  // Overlapping poll with invalid handler.
  Process.poll(79, ProcessType::ProcessPollHandler{});
  TEST_ASSERT_EQUAL_UINT32(0U, s_r22_poll_calls_1);

  rpc_pb_ProcessPollResponse poll_resp =
      rpc_pb_ProcessPollResponse_init_default;
  poll_resp.status = static_cast<uint32_t>(pw::OkStatus().code());
  poll_resp.exit_code = 42U;
  invoke_rpc_response_with_status(ba, kProcSvc, kPollMeth,
                                  rpc_pb_ProcessPollResponse_fields, poll_resp,
                                  pw::OkStatus());
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_poll_calls_1);
  TEST_ASSERT_TRUE(s_r22_poll_status_1.ok());
  TEST_ASSERT_EQUAL_UINT16(42U, s_r22_poll_exit_1);

  // 5. Non-OK RESPONSE status and SERVER_ERROR on poll propagate status.
  s_rpc_status_calls = 0U;
  s_r22_poll_calls_1 = 0U;
  Process.poll(77,
               ProcessType::ProcessPollHandler::create<r22_poll_handler_1>());
  rpc_pb_ProcessPollResponse poll_err_resp =
      rpc_pb_ProcessPollResponse_init_default;
  invoke_rpc_response_with_status(ba, kProcSvc, kPollMeth,
                                  rpc_pb_ProcessPollResponse_fields,
                                  poll_err_resp, pw::Status::NotFound());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::NotFound().code()),
                        static_cast<int>(s_rpc_last_status.code()));
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_poll_calls_1);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::NotFound().code()),
                        static_cast<int>(s_r22_poll_status_1.code()));

  s_rpc_status_calls = 0U;
  s_r22_poll_calls_1 = 0U;
  Process.poll(77,
               ProcessType::ProcessPollHandler::create<r22_poll_handler_1>());
  invoke_rpc_server_error(ba, kProcSvc, kPollMeth, pw::Status::Internal());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Internal().code()),
                        static_cast<int>(s_rpc_last_status.code()));
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_poll_calls_1);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Internal().code()),
                        static_cast<int>(s_r22_poll_status_1.code()));

  // Cover poll on_error and !isRpcTxReady with invalid handler.
  Process.poll(77, ProcessType::ProcessPollHandler{});
  invoke_rpc_server_error(ba, kProcSvc, kPollMeth, pw::Status::Unavailable());
  ba.setTxEnabled(false);
  Process.poll(77, ProcessType::ProcessPollHandler{});
  ba.setTxEnabled(true);

  Bridge.onStatus(BridgeClass::StatusHandler());
}

// Regression (R22): DataStore RPC client must preserve 1-in-flight correlation
// and never pass default-initialized payloads on non-OK status / SERVER_ERROR.
void test_datastore_rpc_correlation_and_status_propagation() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);
  constexpr uint32_t kDsSvc = pw::tokenizer::Hash("rpc.pb.DataStoreService");
  constexpr uint32_t kGetMeth = pw::tokenizer::Hash("Get");

  s_rpc_status_calls = 0U;
  s_rpc_last_status = pw::OkStatus();
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_rpc_status>());
  DataStore.onLost();

  // 1. Overlapping DataStore.get rejects 2nd with ResourceExhausted without
  // clobbering 1st call.
  s_r22_ds_calls_1 = 0U;
  s_r22_ds_first_byte_1 = 0U;
  s_r22_ds_calls_2 = 0U;
  DataStore.get("k1", DataStoreClass::GetHandler::create<r22_ds_handler_1>());
  DataStore.get("k2", DataStoreClass::GetHandler::create<r22_ds_handler_2>());
  TEST_ASSERT_EQUAL_UINT32(0U, s_r22_ds_calls_2);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::ResourceExhausted().code()),
      static_cast<int>(s_rpc_last_status.code()));

  rpc_pb_DatastoreGetResponse ok_resp =
      rpc_pb_DatastoreGetResponse_init_default;
  ok_resp.value.size = 1U;
  ok_resp.value.bytes[0] = 0xABU;
  invoke_rpc_response_with_status(ba, kDsSvc, kGetMeth,
                                  rpc_pb_DatastoreGetResponse_fields, ok_resp,
                                  pw::OkStatus());
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_ds_calls_1);
  TEST_ASSERT_EQUAL_HEX8(0xABU, s_r22_ds_first_byte_1);

  // 2. Non-OK RESPONSE status emits status, does NOT invoke handler, frees
  // slot.
  s_rpc_status_calls = 0U;
  s_r22_ds_calls_1 = 0U;
  DataStore.get("missing",
                DataStoreClass::GetHandler::create<r22_ds_handler_1>());
  rpc_pb_DatastoreGetResponse empty_resp =
      rpc_pb_DatastoreGetResponse_init_default;
  invoke_rpc_response_with_status(ba, kDsSvc, kGetMeth,
                                  rpc_pb_DatastoreGetResponse_fields,
                                  empty_resp, pw::Status::NotFound());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::NotFound().code()),
                        static_cast<int>(s_rpc_last_status.code()));
  TEST_ASSERT_EQUAL_UINT32(0U, s_r22_ds_calls_1);

  // 3. SERVER_ERROR invokes on_error, emits status, frees slot for next call.
  s_rpc_status_calls = 0U;
  DataStore.get("denied",
                DataStoreClass::GetHandler::create<r22_ds_handler_1>());
  invoke_rpc_server_error(ba, kDsSvc, kGetMeth, pw::Status::PermissionDenied());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::PermissionDenied().code()),
                        static_cast<int>(s_rpc_last_status.code()));
  TEST_ASSERT_EQUAL_UINT32(0U, s_r22_ds_calls_1);

  // Subsequent get succeeds after error cleanup.
  DataStore.get("after_err",
                DataStoreClass::GetHandler::create<r22_ds_handler_1>());
  ok_resp.value.bytes[0] = 0xCDU;
  invoke_rpc_response_with_status(ba, kDsSvc, kGetMeth,
                                  rpc_pb_DatastoreGetResponse_fields, ok_resp,
                                  pw::OkStatus());
  TEST_ASSERT_EQUAL_UINT32(1U, s_r22_ds_calls_1);
  TEST_ASSERT_EQUAL_HEX8(0xCDU, s_r22_ds_first_byte_1);

  Bridge.onStatus(BridgeClass::StatusHandler());
}

// Regression (R22): Mailbox RPC client must propagate non-OK status and
// SERVER_ERROR without enqueuing bogus payloads or reporting fake counts, and
// reject overlapping requestRead / requestAvailable cleanly.
void test_mailbox_rpc_correlation_and_status_propagation() {
  BiStream stream;
  reset_bridge_comp(stream);
  auto& ba = TestAccessor::create(Bridge);
  constexpr uint32_t kMbSvc = pw::tokenizer::Hash("rpc.pb.MailboxService");
  constexpr uint32_t kPushMeth = pw::tokenizer::Hash("Push");
  constexpr uint32_t kReadMeth = pw::tokenizer::Hash("Read");
  constexpr uint32_t kAvailMeth = pw::tokenizer::Hash("Available");

  s_rpc_status_calls = 0U;
  s_rpc_last_status = pw::OkStatus();
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_rpc_status>());
  Mailbox.onLost();

  message_callback_called = false;
  available_callback_called = false;
  last_available_count = 99U;
  Mailbox.registerMessageCallback(
      MailboxType::MessageCallback::create<my_message_callback>());
  Mailbox.registerAvailableCallback(
      MailboxType::AvailableCallback::create<my_available_callback>());

  // 1. Overlapping requestRead returns false and emits ResourceExhausted.
  TEST_ASSERT_TRUE(Mailbox.requestRead());
  TEST_ASSERT_FALSE(Mailbox.requestRead());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::ResourceExhausted().code()),
      static_cast<int>(s_rpc_last_status.code()));

  // Non-OK RESPONSE on Read emits status and does NOT enqueue bogus payload.
  s_rpc_status_calls = 0U;
  rpc_pb_MailboxReadResponse bad_read = rpc_pb_MailboxReadResponse_init_default;
  bad_read.content.size = 2U;
  bad_read.content.bytes[0] = 0xDE;
  bad_read.content.bytes[1] = 0xAD;
  invoke_rpc_response_with_status(ba, kMbSvc, kReadMeth,
                                  rpc_pb_MailboxReadResponse_fields, bad_read,
                                  pw::Status::Unavailable());
  Mailbox.process();
  TEST_ASSERT_FALSE(message_callback_called);
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Unavailable().code()),
                        static_cast<int>(s_rpc_last_status.code()));

  // SERVER_ERROR on Read emits status and frees slot.
  s_rpc_status_calls = 0U;
  TEST_ASSERT_TRUE(Mailbox.requestRead());
  invoke_rpc_server_error(ba, kMbSvc, kReadMeth, pw::Status::Internal());
  Mailbox.process();
  TEST_ASSERT_FALSE(message_callback_called);
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Internal().code()),
                        static_cast<int>(s_rpc_last_status.code()));

  // 2. Overlapping requestAvailable returns false and emits ResourceExhausted.
  TEST_ASSERT_TRUE(Mailbox.requestAvailable());
  TEST_ASSERT_FALSE(Mailbox.requestAvailable());
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::ResourceExhausted().code()),
      static_cast<int>(s_rpc_last_status.code()));

  // Non-OK RESPONSE on Available emits status and does NOT call callback.
  s_rpc_status_calls = 0U;
  rpc_pb_MailboxAvailableResponse bad_avail =
      rpc_pb_MailboxAvailableResponse_init_default;
  bad_avail.count = 0U;
  invoke_rpc_response_with_status(ba, kMbSvc, kAvailMeth,
                                  rpc_pb_MailboxAvailableResponse_fields,
                                  bad_avail, pw::Status::PermissionDenied());
  TEST_ASSERT_FALSE(available_callback_called);
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::PermissionDenied().code()),
                        static_cast<int>(s_rpc_last_status.code()));

  // SERVER_ERROR on Available emits status and frees slot.
  s_rpc_status_calls = 0U;
  TEST_ASSERT_TRUE(Mailbox.requestAvailable());
  invoke_rpc_server_error(ba, kMbSvc, kAvailMeth, pw::Status::Unavailable());
  TEST_ASSERT_FALSE(available_callback_called);
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);

  // 3. Push OK response, non-OK response, and SERVER_ERROR paths.
  const etl::array<uint8_t, 2> push_bytes = {0x01U, 0x02U};
  rpc_pb_Empty empty_msg = rpc_pb_Empty_init_default;
  s_rpc_status_calls = 0U;
  Mailbox.push(push_bytes);
  invoke_rpc_response_with_status(ba, kMbSvc, kPushMeth, rpc_pb_Empty_fields,
                                  empty_msg, pw::OkStatus());
  TEST_ASSERT_EQUAL_UINT32(0U, s_rpc_status_calls);

  Mailbox.push(push_bytes);
  invoke_rpc_response_with_status(ba, kMbSvc, kPushMeth, rpc_pb_Empty_fields,
                                  empty_msg, pw::Status::ResourceExhausted());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(pw::Status::ResourceExhausted().code()),
      static_cast<int>(s_rpc_last_status.code()));

  s_rpc_status_calls = 0U;
  Mailbox.push(push_bytes);
  invoke_rpc_server_error(ba, kMbSvc, kPushMeth, pw::Status::Internal());
  TEST_ASSERT_EQUAL_UINT32(1U, s_rpc_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(pw::Status::Internal().code()),
                        static_cast<int>(s_rpc_last_status.code()));

  Bridge.onStatus(BridgeClass::StatusHandler());
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
  RUN_TEST(test_process_edges);
  RUN_TEST(test_console_edges);
  RUN_TEST(test_datastore_edges);
  RUN_TEST(test_filesystem_edges);
  RUN_TEST(test_spi_edges);
  RUN_TEST(test_mailbox_edges);
  RUN_TEST(test_process_rpc_correlation_and_status_propagation);
  RUN_TEST(test_datastore_rpc_correlation_and_status_propagation);
  RUN_TEST(test_mailbox_rpc_correlation_and_status_propagation);
  return UNITY_END();
}
