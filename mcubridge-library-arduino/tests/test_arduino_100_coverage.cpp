#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include <etl/array.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "etl_ext/CounterIterator.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

// Services
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }
void delay(unsigned long ms) { g_test_millis += ms; }

// ETL error handler declaration
namespace etl {
void handle_error(const etl::exception& e);
}

namespace {

using bridge::etl_ext::CounterIterator;
using bridge::test::TestAccessor;

void test_bridge_basic_lifecycle() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  ba.setSynchronized();
  TEST_ASSERT(Bridge.isSynchronized());

  Bridge.enterSafeState();
}

void test_bridge_brute_force_commands() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};

  auto hit = [&](rpc::CommandId id, auto packet, uint32_t tag) {
    bridge::test::set_pb_payload(f, packet, tag);
    ba.dispatch(f);
  };

  // GPIO VALID
  hit(rpc::CommandId::CMD_DIGITAL_WRITE, []() {
    rpc_pb_DigitalWrite p = rpc_pb_DigitalWrite_init_default;
    p.pin = 13;
    p.value = 1;
    return p;
  }(), rpc_pb_RpcPayload_digital_write_tag);

  hit(rpc::CommandId::CMD_ANALOG_WRITE, []() {
    rpc_pb_AnalogWrite p = rpc_pb_AnalogWrite_init_default;
    p.pin = 9;
    p.value = 128;
    return p;
  }(), rpc_pb_RpcPayload_analog_write_tag);

  hit(rpc::CommandId::CMD_DIGITAL_READ, []() {
    rpc_pb_PinRead p = rpc_pb_PinRead_init_default;
    p.pin = 13;
    return p;
  }(), rpc_pb_RpcPayload_digital_read_tag);

  hit(rpc::CommandId::CMD_ANALOG_READ, []() {
    rpc_pb_PinRead p = rpc_pb_PinRead_init_default;
    p.pin = 14;
    return p;
  }(), rpc_pb_RpcPayload_analog_read_tag);

  // GPIO INVALID
  hit(rpc::CommandId::CMD_DIGITAL_WRITE, []() {
    rpc_pb_DigitalWrite p = rpc_pb_DigitalWrite_init_default;
    p.pin = 99;
    p.value = 1;
    return p;
  }(), rpc_pb_RpcPayload_digital_write_tag);

  hit(rpc::CommandId::CMD_SET_PIN_MODE, []() {
    rpc_pb_PinMode p = rpc_pb_PinMode_init_default;
    p.pin = 99;
    p.mode = 1;
    return p;
  }(), rpc_pb_RpcPayload_set_pin_mode_tag);

  // DataStore
  hit(rpc::CommandId::CMD_DATASTORE_PUT, []() {
    rpc_pb_DatastorePut p = rpc_pb_DatastorePut_init_default;
    strncpy(p.key, "k", 32);
    uint8_t v[] = "v";
    rpc::payload::copy_to_pb_bytes(p.value, v, 1);
    return p;
  }(), rpc_pb_RpcPayload_datastore_put_tag);

  hit(rpc::CommandId::CMD_DATASTORE_GET, []() {
    rpc_pb_DatastoreGet p = rpc_pb_DatastoreGet_init_default;
    strncpy(p.key, "k", 32);
    return p;
  }(), rpc_pb_RpcPayload_datastore_get_tag);

  // Mailbox
  hit(rpc::CommandId::CMD_MAILBOX_PUSH, []() {
    rpc_pb_MailboxPush p = rpc_pb_MailboxPush_init_default;
    uint8_t v[] = "v";
    rpc::payload::copy_to_pb_bytes(p.data, v, 1);
    return p;
  }(), rpc_pb_RpcPayload_mailbox_push_tag);

  // Process
  hit(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, []() {
    rpc_pb_ProcessRunAsync p = rpc_pb_ProcessRunAsync_init_default;
    strncpy(p.command, "ls", 64);
    return p;
  }(), rpc_pb_RpcPayload_process_run_async_tag);

  // SPI
  rpc_pb_SpiConfig sc = rpc_pb_SpiConfig_init_default;
  sc.frequency = 4000000;
  sc.bit_order = 1;
  sc.data_mode = 0;
  hit(rpc::CommandId::CMD_SPI_SET_CONFIG, sc, rpc_pb_RpcPayload_spi_config_tag);

  // Core commands
  rpc_pb_Empty empty = rpc_pb_Empty_init_default;
  hit(rpc::CommandId::CMD_GET_FREE_MEMORY, empty, rpc_pb_RpcPayload_get_free_memory_tag);
  hit(rpc::CommandId::CMD_GET_VERSION, empty, rpc_pb_RpcPayload_get_version_tag);
}

void test_bridge_send_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  (void)Bridge.send(
      rpc::CommandId::CMD_GET_VERSION_RESP, 1, []() {
        rpc_pb_VersionResponse p = rpc_pb_VersionResponse_init_default;
        p.major = 1;
        p.minor = 2;
        p.patch = 3;
        return p;
      }());

  // Hit Queue Full
  for (int i = 0; i < rpc::RPC_MAX_PENDING_TX_FRAMES; i++)
    (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 100 + i);
  TEST_ASSERT(ba.isAwaitingAck());
  bool ok = Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 105);
  TEST_ASSERT_FALSE(ok);
}

void test_console_and_misc() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  Console.begin();
  Console.write('X');
  uint8_t d[] = "abc";
  Console.write(d, 3);
  Console.process();

  Bridge.signalXoff();
  Bridge.signalXon();

  rpc::Frame f = {};
  rpc_pb_Empty empty = rpc_pb_Empty_init_default;
  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_ok_tag);
  ba.dispatch(f);

  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_malformed_tag);
  ba.dispatch(f);

  // Trigger etl::handle_error
  etl::exception e("msg", "file", 100);
  etl::handle_error(e);
}

void test_bridge_helpers_coverage() {
  TEST_ASSERT_EQUAL(64, rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION));
  TEST_ASSERT(BridgeClass::is_reliable_cmd(
      (uint16_t)rpc::CommandId::CMD_CONSOLE_WRITE));
  TEST_ASSERT_FALSE(
      BridgeClass::is_reliable_cmd((uint16_t)rpc::CommandId::CMD_GET_VERSION));
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_basic_lifecycle);
  RUN_TEST(test_bridge_brute_force_commands);
  RUN_TEST(test_bridge_send_exhaustive);
  RUN_TEST(test_console_and_misc);
  RUN_TEST(test_bridge_helpers_coverage);
  return UNITY_END();
}
