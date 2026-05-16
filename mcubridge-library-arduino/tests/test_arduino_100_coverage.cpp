#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "fsm/CounterIterator.h"
#include "test_support.h"

// Services
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"

#include <etl/array.h>

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

using bridge::test::TestAccessor;
using bridge::utils::CounterIterator;

class CoverageObserver : public BridgeObserver {
public:
    bool sync_called = false;
    bool lost_called = false;
    void notification(MsgBridgeSynchronized) override { sync_called = true; }
    void notification(MsgBridgeLost) override { lost_called = true; }
};

void test_bridge_basic_lifecycle() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  
  CoverageObserver obs;
  Bridge.registerObserver(obs);
  
  ba.setSynchronized();
  TEST_ASSERT(Bridge.isSynchronized());
  
  Bridge.enterSafeState();
  TEST_ASSERT(obs.lost_called);
}

void test_bridge_brute_force_commands() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  static etl::array<uint8_t, 512> pl_buf;
  rpc::Frame f = {};
  f.payload = etl::span<uint8_t>(pl_buf.data(), pl_buf.size());
  
  auto hit = [&](rpc::CommandId id, auto packet) {
      f.header.command_id = (uint16_t)id;
      f.header.sequence_id++;
      bridge::test::set_pb_payload(f, packet);
      ba.dispatch(f);
  };

  // GPIO VALID
  hit(rpc::CommandId::CMD_DIGITAL_WRITE, rpc::payload::DigitalWrite{13, 1});
  hit(rpc::CommandId::CMD_ANALOG_WRITE, rpc::payload::AnalogWrite{9, 128});
  hit(rpc::CommandId::CMD_DIGITAL_READ, rpc::payload::PinRead{13});
  hit(rpc::CommandId::CMD_ANALOG_READ, rpc::payload::PinRead{14});
  
  // GPIO INVALID
  hit(rpc::CommandId::CMD_DIGITAL_WRITE, rpc::payload::DigitalWrite{99, 1});
  hit(rpc::CommandId::CMD_ANALOG_WRITE, rpc::payload::AnalogWrite{99, 128});
  hit(rpc::CommandId::CMD_DIGITAL_READ, rpc::payload::PinRead{99});
  hit(rpc::CommandId::CMD_ANALOG_READ, rpc::payload::PinRead{99});
  hit(rpc::CommandId::CMD_SET_PIN_MODE, rpc::payload::PinMode{99, 1});

  // DataStore
  uint8_t val[] = "v";
  hit(rpc::CommandId::CMD_DATASTORE_PUT, rpc::payload::DatastorePut{"k", etl::span<const uint8_t>(val, 1)});
  hit(rpc::CommandId::CMD_DATASTORE_GET, rpc::payload::DatastoreGet{"k"});
  
  // Mailbox
  hit(rpc::CommandId::CMD_MAILBOX_PUSH, rpc::payload::MailboxPush{etl::span<const uint8_t>(val, 1)});
  
  f.header.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_READ;
  f.header.payload_length = 0;
  ba.dispatch(f);
  
  f.header.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_AVAILABLE;
  ba.dispatch(f);
  
  // Process
  hit(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, rpc::payload::ProcessRunAsync{"ls"});
  hit(rpc::CommandId::CMD_PROCESS_POLL, rpc::payload::ProcessPoll{123});
  hit(rpc::CommandId::CMD_PROCESS_KILL, rpc::payload::ProcessKill{123});
  
  // SPI
  rpc::payload::SpiConfig sc = {4000000, 1, 0};
  hit(rpc::CommandId::CMD_SPI_SET_CONFIG, sc);
  
  rpc::payload::SpiTransfer st = {};
  st.data = etl::span<const uint8_t>(val, 1);
  hit(rpc::CommandId::CMD_SPI_TRANSFER, st);
  
  // FileSystem
  rpc::payload::FileWrite fw = {};
  fw.path = etl::span<const char>("t.txt", 5);
  fw.data = etl::span<const uint8_t>(val, 1);
  hit(rpc::CommandId::CMD_FILE_WRITE, fw);
  
  rpc::payload::FileRead fr = {};
  fr.path = etl::span<const char>("t.txt", 5);
  hit(rpc::CommandId::CMD_FILE_READ, fr);
  
  rpc::payload::FileRemove frm = {};
  frm.path = etl::span<const char>("t.txt", 5);
  hit(rpc::CommandId::CMD_FILE_REMOVE, frm);

  // Core commands
  hit(rpc::CommandId::CMD_GET_FREE_MEMORY, rpc::payload::FreeMemoryResponse{0});
  hit(rpc::CommandId::CMD_GET_VERSION, rpc::payload::VersionResponse{2, 8, 5});
  hit(rpc::CommandId::CMD_GET_CAPABILITIES, rpc::payload::Capabilities{});
}

void test_bridge_send_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  
  uint8_t data[] = "d";
  
  (void)Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 1, rpc::payload::VersionResponse{1,2,3});
  (void)Bridge.send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, 1, rpc::payload::FreeMemoryResponse{1024});
  (void)Bridge.send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, 1, rpc::payload::Capabilities{});
  (void)Bridge.send(rpc::CommandId::CMD_DIGITAL_READ_RESP, 1, rpc::payload::DigitalReadResponse{1});
  (void)Bridge.send(rpc::CommandId::CMD_ANALOG_READ_RESP, 1, rpc::payload::AnalogReadResponse{512});
  
  rpc::payload::DatastoreGetResponse dgr;
  dgr.value = etl::span<const uint8_t>(data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_GET_RESP, 1, dgr);
  
  rpc::payload::MailboxReadResponse mbr;
  mbr.content = etl::span<const uint8_t>(data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_MAILBOX_READ_RESP, 1, mbr);
  
  rpc::payload::FileReadResponse frr;
  frr.content = etl::span<const uint8_t>(data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 1, frr);
  
  (void)Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP, 1, rpc::payload::ProcessRunAsyncResponse{123});
  
  rpc::payload::ProcessPollResponse ppr;
  ppr.stdout_data = etl::span<const uint8_t>(data, 1);
  ppr.stderr_data = etl::span<const uint8_t>(data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_PROCESS_POLL_RESP, 1, ppr);
  
  rpc::payload::SpiTransferResponse strr;
  strr.data = etl::span<const uint8_t>(data, 1);
  (void)Bridge.send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, 1, strr);

  // 1. Hit Queue Full
  for(int i=0; i<bridge::config::MAX_PENDING_TX_FRAMES; i++) (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 100+i);
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
  f.header.command_id = (uint16_t)rpc::CommandId::CMD_CONSOLE_WRITE | rpc::RPC_CMD_FLAG_COMPRESSED;
  uint8_t comp_data[] = {0x03}; // Truncated RLE
  f.payload = etl::span<const uint8_t>(comp_data, 1);
  f.header.payload_length = 1;
  ba.dispatch(f);
  
  // 4. Trigger etl::handle_error
  etl::exception e("msg", "file", 100);
  etl::handle_error(e);
}

void test_bridge_helpers_coverage() {
  TEST_ASSERT_EQUAL(64, rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION));
  TEST_ASSERT(BridgeClass::is_reliable_cmd((uint16_t)rpc::CommandId::CMD_CONSOLE_WRITE));
  TEST_ASSERT_FALSE(BridgeClass::is_reliable_cmd((uint16_t)rpc::CommandId::CMD_GET_VERSION));
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_basic_lifecycle);
  RUN_TEST(test_bridge_brute_force_commands);
  RUN_TEST(test_bridge_send_exhaustive);
  RUN_TEST(test_console_and_misc);
  RUN_TEST(test_bridge_helpers_coverage);
  return UNITY_END();
}
