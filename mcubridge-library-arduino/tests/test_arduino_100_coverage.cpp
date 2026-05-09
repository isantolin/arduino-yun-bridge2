#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
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

namespace {

using bridge::test::TestAccessor;

void ds_handler(etl::string_view, etl::span<const uint8_t>) {}
void proc_handler(int32_t) {}
void poll_handler(rpc::StatusCode, uint8_t, etl::span<const uint8_t>,
                  etl::span<const uint8_t>) {}
void fs_handler(etl::span<const uint8_t>) {}

void test_bridge_reset_state() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  TEST_ASSERT(ba.isUnsynchronized());
  Bridge.enterSafeState();
  TEST_ASSERT(ba.getStartupStabilizing());
}

void test_bridge_exhaustive_dispatch() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  static etl::array<uint8_t, 256> buf;
  auto dispatch_payload = [&](rpc::CommandId id, auto payload) {
    buf.fill(0);
    msgpack::Encoder enc(buf.data(), buf.size());
    payload.encode(enc);
    rpc::Frame f = {};
    f.header.command_id = (uint16_t)id;
    f.payload = enc.result();
    f.header.payload_length = (uint16_t)f.payload.size();
    ba.dispatch(f);
  };

  auto dispatch_raw = [&](uint16_t id) {
    rpc::Frame f = {};
    f.header.command_id = id;
    ba.dispatch(f);
  };

  for (uint16_t i = 0x40; i <= 0xBF; ++i) dispatch_raw(i);

  dispatch_payload(rpc::CommandId::CMD_GET_VERSION, rpc::payload::VersionResponse{2, 8, 5});
  dispatch_payload(rpc::CommandId::CMD_GET_FREE_MEMORY, rpc::payload::FreeMemoryResponse{1024});
  dispatch_payload(rpc::CommandId::CMD_GET_CAPABILITIES, rpc::payload::Capabilities{1, 1, 20, 6, 0xFFFF});
  dispatch_payload(rpc::CommandId::CMD_SET_BAUDRATE, rpc::payload::SetBaudratePacket{57600});
  dispatch_payload(rpc::CommandId::CMD_ENTER_BOOTLOADER, rpc::payload::EnterBootloader{rpc::RPC_BOOTLOADER_MAGIC});

  dispatch_payload(rpc::CommandId::CMD_SET_PIN_MODE, rpc::payload::PinMode{13, 1});
  dispatch_payload(rpc::CommandId::CMD_DIGITAL_WRITE, rpc::payload::DigitalWrite{13, 1});
  dispatch_payload(rpc::CommandId::CMD_ANALOG_WRITE, rpc::payload::AnalogWrite{3, 128});
  dispatch_payload(rpc::CommandId::CMD_DIGITAL_READ, rpc::payload::PinRead{13});
  dispatch_payload(rpc::CommandId::CMD_ANALOG_READ, rpc::payload::PinRead{0});

  etl::array<uint8_t, 4> d = {'d', 'a', 't', 'a'};
  dispatch_payload(rpc::CommandId::CMD_DATASTORE_GET_RESP, rpc::payload::DatastoreGetResponse{etl::span<const uint8_t>(d.data(), d.size())});
  dispatch_payload(rpc::CommandId::CMD_MAILBOX_PUSH, rpc::payload::MailboxPush{etl::span<const uint8_t>(d.data(), d.size())});
  dispatch_payload(rpc::CommandId::CMD_MAILBOX_READ_RESP, rpc::payload::MailboxReadResponse{etl::span<const uint8_t>(d.data(), d.size())});
  dispatch_payload(rpc::CommandId::CMD_FILE_WRITE, rpc::payload::FileWrite{"test", etl::span<const uint8_t>(d.data(), d.size())});
  dispatch_payload(rpc::CommandId::CMD_FILE_READ, rpc::payload::FileRead{"test"});
  dispatch_payload(rpc::CommandId::CMD_FILE_REMOVE, rpc::payload::FileRemove{"test"});
  dispatch_payload(rpc::CommandId::CMD_FILE_READ_RESP, rpc::payload::FileReadResponse{etl::span<const uint8_t>(d.data(), d.size())});
  dispatch_payload(rpc::CommandId::CMD_PROCESS_KILL, rpc::payload::ProcessKill{123});
  dispatch_payload(rpc::CommandId::CMD_PROCESS_POLL_RESP, rpc::payload::ProcessPollResponse{123, 0, etl::span<const uint8_t>(d.data(), d.size()), etl::span<const uint8_t>(d.data(), d.size())});
  dispatch_payload(rpc::CommandId::CMD_SPI_BEGIN, rpc::payload::SpiConfig{0, 0, 1000000});
  dispatch_payload(rpc::CommandId::CMD_SPI_TRANSFER, rpc::payload::SpiTransfer{etl::span<const uint8_t>(d.data(), d.size())});
  dispatch_payload(rpc::CommandId::CMD_SPI_END, rpc::payload::SpiConfig{0, 0, 0});
  dispatch_payload(rpc::CommandId::CMD_SPI_SET_CONFIG, rpc::payload::SpiConfig{0, 0, 1000000});

  etl::array<uint8_t, 1> d1 = {0};
  dispatch_payload(rpc::CommandId::CMD_MAILBOX_PUSH, rpc::payload::MailboxPush{etl::span<const uint8_t>(d1.data(), d1.size())});
}

void test_bridge_transmit_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  Bridge.emitStatus(rpc::StatusCode::STATUS_OK);
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, "err");
  Bridge.emitStatus(rpc::StatusCode::STATUS_ACK);
  Bridge.signalXoff();
  Bridge.signalXon();

  etl::array<uint8_t, 4> d = {'d', 'a', 't', 'a'};
  (void)Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE, 1, etl::span<const uint8_t>(d.data(), d.size()));
  (void)Bridge.send(rpc::CommandId::CMD_DIGITAL_WRITE, 2, rpc::payload::DigitalWrite{13, 1});
}

void test_bridge_fsm_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeComplete());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
}

void test_services_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  Console.begin();
  Console.write('A');
  etl::array<uint8_t, 3> abc = {'A', 'B', 'C'};
  Console.write(abc.data(), 3);
  Console.available();
  Console.read();
  Console.peek();
  Console.process();

  DataStore.set("k", etl::span<const uint8_t>());
  DataStore.get("k", etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<ds_handler>());

  Mailbox.push(etl::span<const uint8_t>());
  Mailbox.requestRead();
  Mailbox.requestAvailable();
  Mailbox.signalProcessed();

  FileSystem.write("p", etl::span<const uint8_t>());
  FileSystem.read("p", FileSystemClass::FileSystemReadHandler::create<fs_handler>());
  FileSystem.remove("p");

  Process.runAsync("c", etl::span<const etl::string_view>(), etl::delegate<void(int32_t)>::create<proc_handler>());
  Process.poll(1, ProcessClass::ProcessPollHandler::create<poll_handler>());
  Process.kill(1);
  Process.reset();
}

void test_bridge_error_handling() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 8> c = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
  ba._onPacketReceived(etl::span<const uint8_t>(c.data(), c.size()));
}

void test_bridge_compressed() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 128> buf;
  buf.fill(0);
  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = 0x4001; 
  ba.dispatch(f);
}

void test_bridge_hal_callbacks() {
  bridge::hal::getArchId();
  uint8_t d, a;
  bridge::hal::getPinCounts(d, a);
  bridge::hal::getCapabilities();
}

void test_bridge_packet_rx_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 3> secret = {1, 2, 3};
  Bridge.begin(115200, reinterpret_cast<const char*>(secret.data()));

  etl::array<uint8_t, 1> d = {0};
  ba._onPacketReceived(etl::span<const uint8_t>(d.data(), d.size()));
}

void test_bridge_dispatch_all() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  bridge::router::CommandContext ctx(nullptr, 0xFFFF, 0, false, false);
  ba.handleGetVersion(ctx);
}

void test_bridge_api_extended() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  Bridge.flushStream();
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_reset_state);
  RUN_TEST(test_bridge_exhaustive_dispatch);
  RUN_TEST(test_bridge_transmit_exhaustive);
  RUN_TEST(test_bridge_fsm_exhaustive);
  RUN_TEST(test_services_exhaustive);
  RUN_TEST(test_bridge_error_handling);
  RUN_TEST(test_bridge_compressed);
  RUN_TEST(test_bridge_hal_callbacks);
  RUN_TEST(test_bridge_packet_rx_exhaustive);
  RUN_TEST(test_bridge_dispatch_all);
  RUN_TEST(test_bridge_api_extended);
  return UNITY_END();
}
