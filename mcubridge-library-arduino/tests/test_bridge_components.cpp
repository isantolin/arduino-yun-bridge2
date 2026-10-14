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
  return UNITY_END();
}