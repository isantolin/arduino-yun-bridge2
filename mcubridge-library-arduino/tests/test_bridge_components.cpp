#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/Process.h"
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
}

void test_process_api() {
  BiStream stream;
  reset_bridge_comp(stream);

#if BRIDGE_ENABLE_PROCESS
  Process.reset();
#endif
}

void test_console_api() {
  BiStream stream;
  reset_bridge_comp(stream);
  Console.begin();
  Console.write('A');
}

void test_datastore_api() {
  BiStream stream;
  reset_bridge_comp(stream);
#if BRIDGE_ENABLE_DATASTORE
// No begin needed
#endif
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
  Mailbox.requestRead();
  Mailbox.requestAvailable();
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

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_all_handlers_coverage);
  RUN_TEST(test_process_api);
  RUN_TEST(test_console_api);
  RUN_TEST(test_datastore_api);
  RUN_TEST(test_mailbox_api);
  return UNITY_END();
}