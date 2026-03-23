#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/mcubridge.pb.h"
#include "test_support.h"
#include "services/FileSystem.h"
#include "services/Process.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/SPIService.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {
BiStream g_null_stream;
}

BridgeClass Bridge(g_null_stream);
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
#if BRIDGE_ENABLE_SPI
SPIServiceClass SPIService;
#endif
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using bridge::test::TestAccessor;

static void reset_bridge_with_stream(BiStream& stream) {
  reset_bridge_core(Bridge, stream);
  Console.begin();
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();
}

static void restore_bridge_to_serial() {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(Serial);
}

// --- COVERAGE TESTS ---

static void test_all_handlers_coverage() {
  BiStream stream;
  reset_bridge_with_stream(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  bridge::router::CommandContext ctx{&f, 0, false, false, 0};

  // System
  ba.handleGetVersion(ctx);
  ba.handleGetFreeMemory(ctx);
  
  // GPIO
  mcubridge_DigitalWrite dw = mcubridge_DigitalWrite_init_default;
  pb_ostream_t s1 = pb_ostream_from_buffer(payload_buffer.data(), payload_buffer.size());
  pb_encode(&s1, mcubridge_DigitalWrite_fields, &dw);
  f.header.payload_length = s1.bytes_written;
  f.payload = etl::span<const uint8_t>(payload_buffer.data(), s1.bytes_written);
  ba.handleDigitalWrite(ctx);

  mcubridge_PinRead pr = mcubridge_PinRead_init_default;
  pb_ostream_t s2 = pb_ostream_from_buffer(payload_buffer.data(), payload_buffer.size());
  pb_encode(&s2, mcubridge_PinRead_fields, &pr);
  f.header.payload_length = s2.bytes_written;
  f.payload = etl::span<const uint8_t>(payload_buffer.data(), s2.bytes_written);
  ba.handleDigitalRead(ctx);
  ba.handleAnalogRead(ctx);

  // Status
  ba.routeStatusCommand(ctx);
  ba.handleAck(1);
  ba.handleMalformed(1);

  // Services
  ba.routeDataStoreCommand(ctx);
  ba.routeMailboxCommand(ctx);
  ba.routeFileSystemCommand(ctx);
  ba.routeProcessCommand(ctx);
  ba.routeUnknownCommand(ctx);

  // Flow Control
  Bridge.sendXoff();
  Bridge.sendXon();

  restore_bridge_to_serial();
}

static void test_process_api() {
  BiStream stream;
  reset_bridge_with_stream(stream);
  
  Process.runAsync("ls", {}, etl::delegate<void(int16_t)>());
  Process.poll(1, etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>());
  Process.kill(1);
  Process.reset();
  
  restore_bridge_to_serial();
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_all_handlers_coverage);
  RUN_TEST(test_process_api);
  return UNITY_END();
}
