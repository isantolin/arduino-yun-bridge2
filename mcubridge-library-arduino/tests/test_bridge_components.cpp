#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <unity.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <BridgeFaultInjection.h>
#include <etl/algorithm.h>
#include <etl/array.h>
#include <etl/span.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
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

  etl::array<uint8_t, 128> frame_buf;
  size_t sz = 0;

  // Call GetVersion
  sz = build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kRequest,
                            pw::tokenizer::Hash("rpc.pb.SystemService"),
                            pw::tokenizer::Hash("GetVersion"),
                            etl::span<const uint8_t>(), frame_buf);
  TestAccessor::create(Bridge).feedHdlcBytes(
      etl::span<const uint8_t>(frame_buf.data(), sz));
  Mailbox.process();

  // Call GetFreeMemory
  sz = build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kRequest,
                            pw::tokenizer::Hash("rpc.pb.SystemService"),
                            pw::tokenizer::Hash("GetFreeMemory"),
                            etl::span<const uint8_t>(), frame_buf);
  TestAccessor::create(Bridge).feedHdlcBytes(
      etl::span<const uint8_t>(frame_buf.data(), sz));
  Mailbox.process();

  // Call GetCapabilities
  sz = build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kRequest,
                            pw::tokenizer::Hash("rpc.pb.SystemService"),
                            pw::tokenizer::Hash("GetCapabilities"),
                            etl::span<const uint8_t>(), frame_buf);
  TestAccessor::create(Bridge).feedHdlcBytes(
      etl::span<const uint8_t>(frame_buf.data(), sz));
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
  // bridge.MailboxService/Push (request from MPU)
  {
    rpc_pb_MailboxPush push_msg = rpc_pb_MailboxPush_init_default;
    push_msg.data.size = 2;
    push_msg.data.bytes[0] = 0x11;
    push_msg.data.bytes[1] = 0x22;
    uint8_t payload_buf[64];
    pb_ostream_t pbos =
        pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
    TEST_ASSERT_TRUE(pb_encode(&pbos, rpc_pb_MailboxPush_fields, &push_msg));
    etl::array<uint8_t, 128> hdlc_buf;
    size_t sz = build_hdlc_rpc_frame(
        1, pw::rpc_avr::PacketType::kRequest,
        pw::tokenizer::Hash("rpc.pb.MailboxService"),
        pw::tokenizer::Hash("Push"),
        etl::span<const uint8_t>(payload_buf, pbos.bytes_written), hdlc_buf);
    TestAccessor::create(Bridge).feedHdlcBytes(
        etl::span<const uint8_t>(hdlc_buf.data(), sz));
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
    uint8_t payload_buf[64];
    pb_ostream_t pbos =
        pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
    TEST_ASSERT_TRUE(
        pb_encode(&pbos, rpc_pb_MailboxReadResponse_fields, &read_resp));
    pw::rpc_avr::RpcPacket pkt;
    pkt.type = pw::rpc_avr::PacketType::kResponse;
    pkt.service_id = pw::tokenizer::Hash("rpc.pb.MailboxService");
    pkt.method_id = pw::tokenizer::Hash("Read");
    pkt.payload = pw::ConstByteSpan(
        reinterpret_cast<const pw::byte*>(payload_buf), pbos.bytes_written);
    TestAccessor::create(Bridge).invokeRpcResponse(pkt);
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
    uint8_t payload_buf[32];
    pb_ostream_t pbos =
        pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
    TEST_ASSERT_TRUE(
        pb_encode(&pbos, rpc_pb_MailboxAvailableResponse_fields, &avail_resp));
    pw::rpc_avr::RpcPacket pkt;
    pkt.type = pw::rpc_avr::PacketType::kResponse;
    pkt.service_id = pw::tokenizer::Hash("rpc.pb.MailboxService");
    pkt.method_id = pw::tokenizer::Hash("Available");
    pkt.payload = pw::ConstByteSpan(
        reinterpret_cast<const pw::byte*>(payload_buf), pbos.bytes_written);
    TestAccessor::create(Bridge).invokeRpcResponse(pkt);
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
  uint8_t pl_buf[64];
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf, sizeof(pl_buf));
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc_pb_PinSubscribeRequest_fields, &sub_req));
  etl::array<uint8_t, 128> hdlc_buf;
  const size_t sz = build_hdlc_rpc_frame(
      1, pw::rpc_avr::PacketType::kRequest,
      pw::tokenizer::Hash("rpc.pb.GpioService"),
      pw::tokenizer::Hash("SubscribePins"),
      etl::span<const uint8_t>(pl_buf, pbos.bytes_written), hdlc_buf);
  ba.feedHdlcBytes(etl::span<const uint8_t>(hdlc_buf.data(), sz));

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
  FileSystem.read(
      "test.txt",
      etl::delegate<void(etl::span<const uint8_t>)>::create<dummy_fs_read>());
  FileSystem.write("test.txt", etl::span<const uint8_t>(mbox_data, 3));
  FileSystem.remove("test.txt");

  rpc_pb_FileReadResponse fr_p = rpc_pb_FileReadResponse_init_default;
  bridge::pb::copy_bytes(fr_p.content, etl::span<const uint8_t>(mbox_data, 3));
  FileSystem._onResponse(fr_p);

  // Process
  Process.runAsync("ls", etl::span<const etl::string_view>(),
                   etl::delegate<void(int32_t)>::create<dummy_process_run>());
  Process.kill(1);
  Process.poll(1, etl::delegate<void(
                      rpc::StatusCode, uint16_t, etl::span<const uint8_t>,
                      etl::span<const uint8_t>)>::create<dummy_process_poll>());

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
  SPIService.transfer(etl::span<uint8_t>(spi_buf, 2));

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

static int32_t s_captured_pid = 0;
static void capture_async_handler(int32_t pid) { s_captured_pid = pid; }
static void capture_poll_handler(rpc::StatusCode, uint16_t,
                                 etl::span<const uint8_t>,
                                 etl::span<const uint8_t>) {}

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

  // 4. Invalid callback should not enqueue
  Process.reset();
  Process.runAsync("ls", {}, ProcessType::ProcessRunHandler{});

  // 5. Oversized arg failure
  etl::array<char, rpc::MAX_PAYLOAD_SIZE + 1> long_arg_storage = {};
  long_arg_storage.fill('a');
  const etl::string_view oversized_arg(long_arg_storage.data(),
                                       rpc::MAX_PAYLOAD_SIZE);
  etl::array<etl::string_view, 2> overflow_args = {oversized_arg,
                                                   etl::string_view("y")};
  Process.runAsync(
      "x", etl::span<const etl::string_view>(overflow_args.data(), 2),
      etl::delegate<void(int32_t)>::create<&capture_async_handler>());
  TEST_ASSERT_EQUAL(-1, s_captured_pid);

  // 6. Prepend-space capacity failure
  etl::array<char, rpc::MAX_PAYLOAD_SIZE> near_full_cmd = {};
  near_full_cmd.fill('c');
  etl::array<etl::string_view, 1> single_arg = {etl::string_view("z")};
  Process.runAsync(
      etl::string_view(near_full_cmd.data(), rpc::MAX_PAYLOAD_SIZE - 1U),
      etl::span<const etl::string_view>(single_arg.data(), 1),
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
      "ls", etl::span<const etl::string_view>(valid_args, 1),
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
  ba.setSharedSecret(etl::span<const uint8_t>(
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
  rpc_pb_FileReadResponse rresp = rpc_pb_FileReadResponse_init_default;
  FileSystem._onResponse(rresp);

  // 3. Read response with valid handler
  FileSystem.read(
      "test.txt",
      FileSystemClass::FileSystemReadHandler::create<&dummy_fs_edge_handler>());
  FileSystem._onResponse(rresp);

  // 4. Synchronized write, read, remove
  uint8_t data[] = {1, 2, 3};
  FileSystem.write("test.txt", etl::span<const uint8_t>(data, 3));
  FileSystem.read(
      "test.txt",
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
  TEST_ASSERT_EQUAL(pw::Status::NotFound().code(),
                    FileSystem._onRead(fread_nonexistent, rresp).code());

  // 7. _onRead timeout via fault injection
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);
  rpc_pb_FileRead fread_valid = {};
  const char* val_path = "test.txt";
  etl::copy_n(val_path, strlen(val_path), fread_valid.path);
  TEST_ASSERT_EQUAL(pw::Status::NotFound().code(),
                    FileSystem._onRead(fread_valid, rresp).code());
  bridge::test::fault::disable(
      bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);

  // 8. Large file multi-chunk reading
  etl::array<uint8_t, 100> large_data;
  large_data.fill(0xAA);
  auto res_w1 = bridge::hal::writeFile("test_large.txt", large_data);
  TEST_ASSERT_TRUE(res_w1.has_value());
  rpc_pb_FileRead req_large = {};
  strncpy(req_large.path, "test_large.txt", sizeof(req_large.path));
  TEST_ASSERT_EQUAL(pw::Status::ResourceExhausted().code(),
                    FileSystem._onRead(req_large, rresp).code());

  // 9. Send-fail branches when tx disabled
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  uint8_t fsdata[] = {1, 2};
  FileSystem.write("send_fail.txt", etl::span<const uint8_t>(fsdata, 2));
  FileSystem.remove("send_fail.txt");
  TEST_ASSERT_EQUAL(pw::Status::ResourceExhausted().code(),
                    FileSystem._onRead(req_large, rresp).code());
  ba.setTxEnabled(true);

  // 10. Empty file (to_copy == 0)
  auto res_w2 = bridge::hal::writeFile("empty.txt", etl::span<const uint8_t>());
  TEST_ASSERT_TRUE(res_w2.has_value());
  rpc_pb_FileRead fread_empty = {};
  const char* empty_path = "empty.txt";
  etl::copy_n(empty_path, strlen(empty_path), fread_empty.path);
  TEST_ASSERT_TRUE(FileSystem._onRead(fread_empty, rresp).ok());

  // 11. Observer onLost
  FileSystem.onLost();
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

  // 4. SPI Transfer via HDLC RPC
  rpc_pb_SpiTransfer spi_req = rpc_pb_SpiTransfer_init_default;
  spi_req.data.size = 0;
  uint8_t req_buf[32];
  pb_ostream_t pos = pb_ostream_from_buffer(req_buf, sizeof(req_buf));
  pb_encode(&pos, rpc_pb_SpiTransfer_fields, &spi_req);
  etl::array<uint8_t, 64> hdlc_buf;
  size_t sz = build_hdlc_rpc_frame(
      1, pw::rpc_avr::PacketType::kRequest,
      pw::tokenizer::Hash("rpc.pb.SpiService"), pw::tokenizer::Hash("Transfer"),
      etl::span<const uint8_t>(req_buf, pos.bytes_written), hdlc_buf);
  ba.feedHdlcBytes(etl::span<const uint8_t>(hdlc_buf.data(), sz));
  ba.setTxEnabled(false);
  ba.feedHdlcBytes(etl::span<const uint8_t>(hdlc_buf.data(), sz));
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

  // Enqueue via _enqueue 5 times to overflow _queue (capacity 4)
  rpc_pb_MailboxPush mb_msg = rpc_pb_MailboxPush_init_default;
  mb_msg.data.size = 3;
  mb_msg.data.bytes[0] = 1;
  mb_msg.data.bytes[1] = 2;
  mb_msg.data.bytes[2] = 3;
  constexpr etl::array<uint8_t, 5> push_attempts{};
  etl::for_each(push_attempts.begin(), push_attempts.end(), [&mb_msg](uint8_t) {
    MailboxClass::_enqueue(
        etl::span<const uint8_t>(mb_msg.data.bytes, mb_msg.data.size));
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
  Mailbox.push(etl::span<const uint8_t>(payload_bytes, 5));
  TEST_ASSERT_FALSE(Mailbox.requestRead());
  TEST_ASSERT_FALSE(Mailbox.requestAvailable());
  Mailbox.signalProcessed(789U);
  ba.setTxEnabled(true);

  // Exercise _serialTask() full HDLC frame decode and kResponse / kServerStream
  // / kClientError
  etl::array<uint8_t, 64> hdlc_buf;
  size_t sz = build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kRequest,
                                   pw::tokenizer::Hash("rpc.pb.SystemService"),
                                   pw::tokenizer::Hash("GetVersion"),
                                   etl::span<const uint8_t>(), hdlc_buf);
  stream.feed(hdlc_buf.data(), sz);
  Bridge.process();

  sz = build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kResponse,
                            pw::tokenizer::Hash("rpc.pb.MailboxService"),
                            pw::tokenizer::Hash("Available"),
                            etl::span<const uint8_t>(), hdlc_buf);
  ba.feedHdlcBytes(etl::span<const uint8_t>(hdlc_buf.data(), sz));

  sz = build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kServerStream,
                            pw::tokenizer::Hash("rpc.pb.MailboxService"),
                            pw::tokenizer::Hash("Read"),
                            etl::span<const uint8_t>(), hdlc_buf);
  ba.feedHdlcBytes(etl::span<const uint8_t>(hdlc_buf.data(), sz));

  sz = build_hdlc_rpc_frame(1, pw::rpc_avr::PacketType::kClientError,
                            pw::tokenizer::Hash("rpc.pb.MailboxService"),
                            pw::tokenizer::Hash("Read"),
                            etl::span<const uint8_t>(), hdlc_buf);
  ba.feedHdlcBytes(etl::span<const uint8_t>(hdlc_buf.data(), sz));

  // Unknown method_id fallthroughs in RPC services and _dispatchRpcResponse
  constexpr uint32_t kUnknownMethod = 0xDEADBEEFU;
  auto send_rpc = [&](pw::rpc_avr::PacketType ptype, uint32_t srv,
                      uint32_t meth,
                      etl::span<const uint8_t> pl =
                          etl::span<const uint8_t>()) {
    const size_t n = build_hdlc_rpc_frame(1, ptype, srv, meth, pl, hdlc_buf);
    ba.feedHdlcBytes(etl::span<const uint8_t>(hdlc_buf.data(), n));
  };
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.GpioService"), kUnknownMethod);
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.ClockSyncService"), kUnknownMethod);
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.SpiService"), kUnknownMethod);
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.FileSystemService"), kUnknownMethod);
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.MailboxService"), kUnknownMethod);
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Read"));
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Available"));
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Processed"));

  send_rpc(pw::rpc_avr::PacketType::kResponse,
           pw::tokenizer::Hash("rpc.pb.MailboxService"), kUnknownMethod);
  send_rpc(pw::rpc_avr::PacketType::kResponse,
           pw::tokenizer::Hash("rpc.pb.ProcessService"), kUnknownMethod);
  send_rpc(pw::rpc_avr::PacketType::kResponse,
           pw::tokenizer::Hash("rpc.pb.FileSystemService"), kUnknownMethod);

  // Malformed protobuf payloads for SystemService::LinkSync and
  // MailboxService::Push
  const uint8_t bad_pb[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                            0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.SystemService"),
           pw::tokenizer::Hash("LinkSync"),
           etl::span<const uint8_t>(bad_pb, sizeof(bad_pb)));
  send_rpc(pw::rpc_avr::PacketType::kRequest,
           pw::tokenizer::Hash("rpc.pb.MailboxService"),
           pw::tokenizer::Hash("Push"),
           etl::span<const uint8_t>(bad_pb, sizeof(bad_pb)));

  Console.pushBytes(nullptr, 5);
  Console.pushBytes(payload_bytes, 0);
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
  return UNITY_END();
}
