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

  static etl::array<uint8_t, 512> pl_buf;
  rpc::Frame f = {};
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> f_buf;
  f.payload = etl::span<uint8_t>(f_buf.data(), f_buf.size());
  f.payload = etl::span<uint8_t>(pl_buf.data(), pl_buf.size());

  auto hit = [&](rpc::CommandId id, auto packet) {
    f.header.command_id = (uint16_t)id;
    f.header.sequence_id++;
    bridge::test::set_pb_payload(f, packet);
    ba.dispatch(f);
  };

  // GPIO VALID
  hit(rpc::CommandId::CMD_DIGITAL_WRITE, []() {
    rpc::payload::DigitalWrite p;
    p.pb_msg.pin = 13;
    p.pb_msg.value = 1;
    return p;
  }());
  hit(rpc::CommandId::CMD_ANALOG_WRITE, []() {
    rpc::payload::AnalogWrite p;
    p.pb_msg.pin = 9;
    p.pb_msg.value = 128;
    return p;
  }());
  hit(rpc::CommandId::CMD_DIGITAL_READ, []() {
    rpc::payload::PinRead p;
    p.pb_msg.pin = 13;
    return p;
  }());
  hit(rpc::CommandId::CMD_ANALOG_READ, []() {
    rpc::payload::PinRead p;
    p.pb_msg.pin = 14;
    return p;
  }());

  // GPIO INVALID
  hit(rpc::CommandId::CMD_DIGITAL_WRITE, []() {
    rpc::payload::DigitalWrite p;
    p.pb_msg.pin = 99;
    p.pb_msg.value = 1;
    return p;
  }());
  hit(rpc::CommandId::CMD_ANALOG_WRITE, []() {
    rpc::payload::AnalogWrite p;
    p.pb_msg.pin = 99;
    p.pb_msg.value = 128;
    return p;
  }());
  hit(rpc::CommandId::CMD_DIGITAL_READ, []() {
    rpc::payload::PinRead p;
    p.pb_msg.pin = 99;
    return p;
  }());
  hit(rpc::CommandId::CMD_ANALOG_READ, []() {
    rpc::payload::PinRead p;
    p.pb_msg.pin = 99;
    return p;
  }());
  hit(rpc::CommandId::CMD_SET_PIN_MODE, []() {
    rpc::payload::PinMode p;
    p.pb_msg.pin = 99;
    p.pb_msg.mode = 1;
    return p;
  }());

  // DataStore
  uint8_t val[] = "v";
  hit(rpc::CommandId::CMD_DATASTORE_PUT, []() {
    rpc::payload::DatastorePut p;
    strncpy(p.pb_msg.key, "k", 32);
    uint8_t v[] = "v";
    rpc::payload::copy_to_pb_bytes(p.pb_msg.value, v, 1);
    return p;
  }());
  hit(rpc::CommandId::CMD_DATASTORE_GET, []() {
    rpc::payload::DatastoreGet p;
    strncpy(p.pb_msg.key, "k", 32);
    return p;
  }());

  // Mailbox
  hit(rpc::CommandId::CMD_MAILBOX_PUSH, []() {
    rpc::payload::MailboxPush p;
    uint8_t v[] = "v";
    rpc::payload::copy_to_pb_bytes(p.pb_msg.data, v, 1);
    return p;
  }());

  f.header.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_READ;
  f.header.payload_length = 0;
  ba.dispatch(f);

  f.header.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_AVAILABLE;
  ba.dispatch(f);

  // Process
  hit(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, []() {
    rpc::payload::ProcessRunAsync p;
    strncpy(p.pb_msg.command, "ls", 64);
    return p;
  }());
  hit(rpc::CommandId::CMD_PROCESS_POLL, []() {
    rpc::payload::ProcessPoll p;
    p.pb_msg.pid = 123;
    return p;
  }());
  hit(rpc::CommandId::CMD_PROCESS_KILL, []() {
    rpc::payload::ProcessKill p;
    p.pb_msg.pid = 123;
    return p;
  }());

  // SPI
  rpc::payload::SpiConfig sc;
  sc.pb_msg.frequency = 4000000;
  sc.pb_msg.bit_order = 1;
  sc.pb_msg.data_mode = 0;
  hit(rpc::CommandId::CMD_SPI_SET_CONFIG, sc);

  rpc::payload::SpiTransfer st = {};
  rpc::payload::copy_to_pb_bytes(st.pb_msg.data, val, 1);
  hit(rpc::CommandId::CMD_SPI_TRANSFER, st);

  // FileSystem
  rpc::payload::FileWrite fw = {};
  strncpy(fw.pb_msg.path, "t.txt", sizeof(fw.pb_msg.path));
  rpc::payload::copy_to_pb_bytes(fw.pb_msg.data, val, 1);
  hit(rpc::CommandId::CMD_FILE_WRITE, fw);

  rpc::payload::FileRead fr = {};
  strncpy(fr.pb_msg.path, "t.txt", sizeof(fr.pb_msg.path));
  hit(rpc::CommandId::CMD_FILE_READ, fr);

  rpc::payload::FileRemove frm = {};
  strncpy(frm.pb_msg.path, "t.txt", sizeof(frm.pb_msg.path));
  hit(rpc::CommandId::CMD_FILE_REMOVE, frm);

  // Core commands
  hit(rpc::CommandId::CMD_GET_FREE_MEMORY, []() {
    rpc::payload::FreeMemoryResponse p;
    p.pb_msg.value = 0;
    return p;
  }());
  hit(rpc::CommandId::CMD_GET_VERSION, []() {
    rpc::payload::VersionResponse p;
    p.pb_msg.major = 2;
    p.pb_msg.minor = 8;
    p.pb_msg.patch = 5;
    return p;
  }());
  hit(rpc::CommandId::CMD_GET_CAPABILITIES, rpc::payload::Capabilities{});
}

void test_bridge_send_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint8_t data[] = "d";

  (void)Bridge.send(
      rpc::CommandId::CMD_GET_VERSION_RESP, 1, []() {
        rpc::payload::VersionResponse p;
        p.pb_msg.major = 1;
        p.pb_msg.minor = 2;
        p.pb_msg.patch = 3;
        return p;
      }());
  (void)Bridge.send(
      rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, 1, []() {
        rpc::payload::FreeMemoryResponse p;
        p.pb_msg.value = 1024;
        return p;
      }());
  (void)Bridge.send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, 1,
                    rpc::payload::Capabilities{});
  (void)Bridge.send(
      rpc::CommandId::CMD_DIGITAL_READ_RESP, 1, []() {
        rpc::payload::DigitalReadResponse p;
        p.pb_msg.value = 1;
        return p;
      }());
  (void)Bridge.send(
      rpc::CommandId::CMD_ANALOG_READ_RESP, 1, []() {
        rpc::payload::AnalogReadResponse p;
        p.pb_msg.value = 512;
        return p;
      }());

  rpc::payload::DatastoreGetResponse dgr;
  rpc::payload::copy_to_pb_bytes(dgr.pb_msg.value, data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_GET_RESP, 1, dgr);

  rpc::payload::MailboxReadResponse mbr;
  rpc::payload::copy_to_pb_bytes(mbr.pb_msg.content, data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_READ_RESP, 1, mbr);

  rpc::payload::FileReadResponse frr;
  rpc::payload::copy_to_pb_bytes(frr.pb_msg.content, data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 1, frr);

  (void)Bridge.send(
      rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP, 1, []() {
        rpc::payload::ProcessRunAsyncResponse p;
        p.pb_msg.pid = 123;
        return p;
      }());

  rpc::payload::ProcessPollResponse ppr;
  rpc::payload::copy_to_pb_bytes(ppr.pb_msg.stdout_data, data, 1);
  rpc::payload::copy_to_pb_bytes(ppr.pb_msg.stderr_data, data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_PROCESS_POLL_RESP, 1, ppr);

  rpc::payload::SpiTransferResponse strr;
  rpc::payload::copy_to_pb_bytes(strr.pb_msg.data, data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, 1, strr);

  // 1. Hit Queue Full
  for (int i = 0; i < bridge::config::MAX_PENDING_TX_FRAMES; i++)
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
  f.header.command_id = (uint16_t)rpc::StatusCode::STATUS_OK;
  ba.dispatch(f);

  f.header.command_id = (uint16_t)rpc::StatusCode::STATUS_MALFORMED;
  ba.dispatch(f);

  // Decompression MALFORMED
  f.header.command_id = (uint16_t)rpc::CommandId::CMD_CONSOLE_WRITE |
                        rpc::RPC_CMD_FLAG_COMPRESSED;
  uint8_t comp_data[] = {0x03};  // Truncated RLE
  f.payload = etl::span<const uint8_t>(comp_data, 1);
  f.header.payload_length = 1;
  ba.dispatch(f);

  // 4. Trigger etl::handle_error
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