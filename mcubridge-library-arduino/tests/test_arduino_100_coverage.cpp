#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
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

unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }
void delay(unsigned long ms) { g_test_millis += ms; }

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

void ds_handler(etl::string_view, etl::span<const uint8_t>) {}
void proc_handler(int32_t) {}
void poll_handler(rpc::StatusCode, uint8_t, etl::span<const uint8_t>,
                  etl::span<const uint8_t>) {}
void fs_handler(etl::span<const uint8_t>) {}

void test_bridge_reset_state() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  Bridge._onStartupStabilized();
  TEST_ASSERT(!Bridge.isSynchronized());
  Bridge.enterSafeState();
}

void test_bridge_exhaustive_dispatch() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  simulate_handshake(Bridge, stream);

  static uint8_t buf[256];
  auto dispatch_payload = [&](rpc::CommandId id, auto payload) {
    memset(buf, 0, sizeof(buf));
    msgpack::Encoder enc(buf, sizeof(buf));
    payload.encode(enc);
    stream.feed_frame(id, 1, enc.result());
    Bridge.process();
  };

  auto dispatch_raw = [&](uint16_t id) {
    stream.feed_frame(static_cast<rpc::CommandId>(id), 1, {});
    Bridge.process();
  };

  // Fill the jump table range 0x40 - 0xBF
  for (uint16_t i = 0x40; i <= 0xBF; ++i) dispatch_raw(i);

  // Explicit system payloads
  dispatch_payload(rpc::CommandId::CMD_GET_VERSION,
                   rpc::payload::VersionResponse{2, 8, 5});
  dispatch_payload(rpc::CommandId::CMD_GET_FREE_MEMORY,
                   rpc::payload::FreeMemoryResponse{1024});
  dispatch_payload(rpc::CommandId::CMD_GET_CAPABILITIES,
                   rpc::payload::Capabilities{1, 1, 20, 6, 0xFFFF});
  dispatch_payload(rpc::CommandId::CMD_SET_BAUDRATE,
                   rpc::payload::SetBaudratePacket{57600});
  dispatch_payload(rpc::CommandId::CMD_ENTER_BOOTLOADER,
                   rpc::payload::EnterBootloader{rpc::RPC_BOOTLOADER_MAGIC});

  // Pins
  dispatch_payload(rpc::CommandId::CMD_SET_PIN_MODE,
                   rpc::payload::PinMode{13, 1});
  dispatch_payload(rpc::CommandId::CMD_DIGITAL_WRITE,
                   rpc::payload::DigitalWrite{13, 1});
  dispatch_payload(rpc::CommandId::CMD_ANALOG_WRITE,
                   rpc::payload::AnalogWrite{3, 128});
  dispatch_payload(rpc::CommandId::CMD_DIGITAL_READ, rpc::payload::PinRead{13});
  dispatch_payload(rpc::CommandId::CMD_ANALOG_READ, rpc::payload::PinRead{0});

  // Services
  uint8_t d[] = "data";
  dispatch_payload(
      rpc::CommandId::CMD_DATASTORE_GET_RESP,
      rpc::payload::DatastoreGetResponse{etl::span<const uint8_t>(d, 4)});
  dispatch_payload(
      rpc::CommandId::CMD_MAILBOX_READ_RESP,
      rpc::payload::MailboxReadResponse{etl::span<const uint8_t>(d, 4)});
  dispatch_payload(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP,
                   rpc::payload::MailboxAvailableResponse{10});
  dispatch_payload(rpc::CommandId::CMD_MAILBOX_PUSH,
                   rpc::payload::MailboxPush{etl::span<const uint8_t>(d, 4)});
  dispatch_payload(
      rpc::CommandId::CMD_FILE_READ_RESP,
      rpc::payload::FileReadResponse{etl::span<const uint8_t>(d, 4)});

  rpc::payload::ProcessPollResponse ppr;
  ppr.status = (uint8_t)rpc::StatusCode::STATUS_OK;
  ppr.stdout_data = etl::span<const uint8_t>(d, 4);
  dispatch_payload(rpc::CommandId::CMD_PROCESS_POLL_RESP, ppr);

  dispatch_raw((uint16_t)rpc::CommandId::CMD_SPI_BEGIN);
  dispatch_raw((uint16_t)rpc::CommandId::CMD_SPI_END);
}

void test_bridge_transmit_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, (const char*)nullptr);
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, "err");
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, F("flash"));

  // Pool fill
  uint8_t d[1] = {0};
  for (int i = 0; i < 20; ++i)
    (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, (uint16_t)i,
                           etl::span<const uint8_t>(d, 1));

  // Advance time to hit delay timers
  g_test_millis += 10000;
  Bridge.process();
}

void test_bridge_fsm_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  
  // Feed events to advance FSM naturally
  Bridge._onStartupStabilized();
  simulate_handshake(Bridge, stream);
  
  // Critical send (triggers state wait_ack)
  uint8_t d[1] = {0};
  (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 1, etl::span<const uint8_t>(d, 1));
  
  // Feed ACK
  stream.feed_frame(rpc::StatusCode::STATUS_ACK, 1, {});
  Bridge.process();
}

void test_services_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  uint8_t d[] = "data";
  Console.write(d, 4);
  DataStore.set("k", etl::span<const uint8_t>(d, 4));
  DataStore.get(
      "k", etl::delegate<void(etl::string_view,
                              etl::span<const uint8_t>)>::create<ds_handler>());

  Mailbox.push(etl::span<const uint8_t>(d, 4));
  Mailbox.requestRead();
  Mailbox.requestAvailable();
  Mailbox.signalProcessed();

  // Exercise Mailbox response with data to hit lambda
  rpc::payload::MailboxPush mbp;
  mbp.data = etl::span<const uint8_t>(d, 4);
  Mailbox._onIncomingData(mbp);
  rpc::payload::MailboxReadResponse mbr;
  mbr.content = etl::span<const uint8_t>(d, 4);
  Mailbox._onIncomingData(mbr);

  FileSystem.write("f", etl::span<const uint8_t>(d, 4));
  FileSystem.read(
      "f", etl::delegate<void(etl::span<const uint8_t>)>::create<fs_handler>());
  FileSystem.remove("f");

  Process.runAsync("ls", etl::span<const etl::string_view>(),
                   etl::delegate<void(int32_t)>::create<proc_handler>());
  Process.poll(
      1, etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>,
                            etl::span<const uint8_t>)>::create<poll_handler>());
  Process.kill(1);

  SPIService.begin();
  SPIService.setConfig(rpc::payload::SpiConfig{1, 1, 1});
  SPIService.end();
}

void test_bridge_error_handling() {
  etl::exception e("test", "file", 1);
  bridge::SafeStatePolicy::handle(Bridge, e);
}

void test_bridge_compressed() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  simulate_handshake(Bridge, stream);

  // Send a frame with compressed bit set
  stream.feed_frame(static_cast<rpc::CommandId>((uint16_t)rpc::CommandId::CMD_CONSOLE_WRITE |
                        rpc::RPC_CMD_FLAG_COMPRESSED), 10, {});
  Bridge.process();
}

void test_bridge_hal_callbacks() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // Advance time and process to hit internal timer-driven logic
  g_test_millis += 10000;
  Bridge.process();
}

void test_bridge_packet_rx_exhaustive() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // 1. Corrupt Frame (Parser failure)
  uint8_t c[] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
  Bridge._onPacketReceived(etl::span<const uint8_t>(c, 8));

  // 2. Valid frame but duplicate
  simulate_handshake(Bridge, stream);
  rpc::Frame f = {};
  f.header.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION;
  f.header.sequence_id = 42;
  uint8_t buf[128];
  size_t len = rpc::FrameParser::serialize(f, etl::span<uint8_t>(buf, 128));
  Bridge._onPacketReceived(etl::span<const uint8_t>(buf, len));
  Bridge._onPacketReceived(etl::span<const uint8_t>(buf, len));  // Duplicate
}

void test_bridge_dispatch_all() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  simulate_handshake(Bridge, stream);

  for (uint16_t i = 0x40; i <= 0xBF; ++i) {
    stream.feed_frame(static_cast<rpc::CommandId>(i), 1, {});
    Bridge.process();
  }
}

void test_bridge_api_extended() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);

  // sendFrame variants
  uint8_t d[] = {0};
  (void)Bridge.sendFrame(rpc::CommandId::CMD_XOFF);
  (void)Bridge.sendFrame(rpc::StatusCode::STATUS_ACK, 42,
                   etl::span<const uint8_t>(d, 1));
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
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
