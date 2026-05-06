#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "test_support.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"
#include "services/FileSystem.h"
#include "hal/hal.h"
#include "hal/progmem_compat.h"
#include <etl/array.h>

// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

namespace {
using bridge::test::TestAccessor;

void dummy_datastore_get(etl::string_view k, etl::span<const uint8_t> v) { (void)k; (void)v; }
void dummy_fs_read(etl::span<const uint8_t> v) { (void)v; }
void dummy_process_run(int32_t p) { (void)p; }
void dummy_process_poll(rpc::StatusCode s, uint8_t n, etl::span<const uint8_t> st, etl::span<const uint8_t> se) { (void)s; (void)n; (void)st; (void)se; }
void dummy_command_handler(const rpc::Frame& f) { (void)f; }

void test_bridge_coverage() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Unknown Command to trigger fallback O(1) branch
  rpc::Frame f_unknown = {};
  f_unknown.header.command_id = 999; 
  ba.dispatch(f_unknown);
  
  // onUnknownCommand with handler
  Bridge.onCommand(etl::delegate<void(const rpc::Frame&)>::create<dummy_command_handler>());
  ba.dispatch(f_unknown);
  Bridge.onCommand(etl::delegate<void(const rpc::Frame&)>::create<nullptr>());

  // 2. Duplicate Sequence ID for _withResponse (e.g. CMD_GET_VERSION)
  ba.setSynchronized();
  rpc::Frame f_ver = {};
  f_ver.header.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION;
  f_ver.header.sequence_id = ba._last_rx_sequence_id();
  ba.dispatch(f_ver);

  // Trigger send<T> coverage directly
  (void)Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 0, rpc::payload::VersionResponse{1,0,0});
  
  // Trigger _handlePinRead error path (invalid pin)
  rpc::Frame f_pin = {};
  f_pin.header.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_READ;
  bridge::test::set_pb_payload(f_pin, rpc::payload::PinRead{255});
  ba.dispatch(f_pin);

  // Trigger _withPayloadAck error path (malformed payload)
  rpc::Frame f_malformed = {};
  f_malformed.header.command_id = (uint16_t)rpc::CommandId::CMD_SET_PIN_MODE;
  f_malformed.header.payload_length = 0; // Malformed for PinMode
  ba.dispatch(f_malformed);

  // Trigger _withPayload error path (malformed payload)
  rpc::Frame f_malformed2 = {};
  f_malformed2.header.command_id = (uint16_t)rpc::CommandId::CMD_SET_BAUDRATE;
  f_malformed2.header.payload_length = 0; // Malformed for SetBaudrate
  ba.dispatch(f_malformed2);

  // Trigger _sendPbResponse error path (payload too large)
  struct LargePayload {
      bool encode(msgpack::Encoder& enc) const { (void)enc; return false; }
  };
  (void)Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 0, LargePayload{});

  // 3. Console Success Paths
  Console.begin();
  (void)Console.write('a');
  (void)Console.write((const uint8_t*)"abc", 3);
  // Fill TX buffer to trigger process()
  for(int i=0; i<bridge::config::CONSOLE_TX_BUFFER_SIZE + 1; i++) (void)Console.write('x');
  Console.flush();
  
  rpc::payload::ConsoleWrite cmsg;
  uint8_t cdata[] = "hello";
  cmsg.data = etl::span<const uint8_t>(cdata, 5);
  ba.invokeConsolePush(cmsg);
  Console.available();
  Console.read();
  Console.peek();

  // 4. DataStore Success Paths
  uint8_t ds_val[] = {1, 2};
  (void)DataStore.set("key", etl::span<const uint8_t>(ds_val, 2));
  DataStore.get("key", etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<dummy_datastore_get>());
  rpc::payload::DatastoreGetResponse ds_get;
  ds_get.value = etl::span<const uint8_t>(ds_val, 2);
  DataStore._onResponse(ds_get);

  // 5. Mailbox Success Paths
  uint8_t mbox_data[bridge::config::MAILBOX_RX_BUFFER_SIZE + 1];
  memset(mbox_data, 0, sizeof(mbox_data));
  (void)Mailbox.push(etl::span<const uint8_t>(mbox_data, 3));
  rpc::payload::MailboxPush mpush;
  mpush.data = etl::span<const uint8_t>(mbox_data, sizeof(mbox_data));
  Mailbox._onIncomingData(mpush); // Trigger full()
  rpc::payload::MailboxReadResponse mread;
  mread.content = etl::span<const uint8_t>(mbox_data, sizeof(mbox_data));
  Mailbox._onIncomingData(mread); // Trigger full()
  rpc::payload::MailboxAvailableResponse mavl;
  mavl.count = 3;
  Mailbox._onAvailableResponse(mavl);
  Mailbox.notification(MsgBridgeLost{});
  Mailbox.requestRead();
  Mailbox.requestAvailable();
  Mailbox.signalProcessed();

  // 6. Pin Handler Coverage via Dispatch
  rpc::Frame f_dw = {}; f_dw.header.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_WRITE; f_dw.header.sequence_id = 11;
  bridge::test::set_pb_payload(f_dw, rpc::payload::DigitalWrite{13, 1});
  ba.dispatch(f_dw);

  rpc::Frame f_aw = {}; f_aw.header.command_id = (uint16_t)rpc::CommandId::CMD_ANALOG_WRITE; f_aw.header.sequence_id = 12;
  bridge::test::set_pb_payload(f_aw, rpc::payload::AnalogWrite{13, 128});
  ba.dispatch(f_aw);

  rpc::Frame f_pm = {}; f_pm.header.command_id = (uint16_t)rpc::CommandId::CMD_SET_PIN_MODE; f_pm.header.sequence_id = 15;
  bridge::test::set_pb_payload(f_pm, rpc::payload::PinMode{13, 1});
  ba.dispatch(f_pm);

  rpc::Frame f_dr = {}; f_dr.header.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_READ; f_dr.header.sequence_id = 13;
  bridge::test::set_pb_payload(f_dr, rpc::payload::PinRead{13});
  ba.dispatch(f_dr);

  rpc::Frame f_ar = {}; f_ar.header.command_id = (uint16_t)rpc::CommandId::CMD_ANALOG_READ; f_ar.header.sequence_id = 14;
  bridge::test::set_pb_payload(f_ar, rpc::payload::PinRead{13});
  ba.dispatch(f_ar);

  // 7. SPI
#if BRIDGE_ENABLE_SPI
  SPIService.begin();
  rpc::payload::SpiConfig spi_cfg;
  spi_cfg.frequency = 1000000;
  spi_cfg.bit_order = 1;
  spi_cfg.data_mode = 0;
  SPIService.setConfig(spi_cfg);
  uint8_t spi_buf[2] = {0, 0};
  SPIService.transfer(etl::span<uint8_t>(spi_buf, 2));
  SPIService.transfer(etl::span<uint8_t>());
  SPIService.end();
#endif

  // 8. FileSystem
  FileSystem.read("test.txt", etl::delegate<void(etl::span<const uint8_t>)>::create<dummy_fs_read>());
  FileSystem.write("test.txt", etl::span<const uint8_t>(ds_val, 2));
  FileSystem.remove("test.txt");
  rpc::payload::FileReadResponse f_read;
  f_read.content = etl::span<const uint8_t>(ds_val, 2);
  FileSystem._onResponse(f_read);
  rpc::payload::FileWrite f_write;
  FileSystem._onWrite(f_write);
  rpc::payload::FileRemove f_remove;
  FileSystem._onRemove(f_remove);
  
  // 9. Process
  ProcessClass::runAsync("ls", etl::span<const etl::string_view>(), etl::delegate<void(int32_t)>::create<dummy_process_run>());
  ProcessClass::kill(1);
  Process.poll(1, etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>::create<dummy_process_poll>());

  rpc::payload::ProcessKill pk;
  ProcessClass::_kill(pk);
  rpc::payload::ProcessRunAsyncResponse prar;
  ProcessClass::_onRunAsyncResponse(prar);
  rpc::payload::ProcessPollResponse ppr;
  ProcessClass::_onPollResponse(ppr);
  Process.reset();

  // 10. hal implementations (progmem, free memory, weak defaults)
  bridge::hal::getFreeMemory();
  bridge::hal::init();
  bridge::hal::hasSD();
  bridge::hal::writeFile("test.txt", etl::span<const uint8_t>());
  uint8_t read_buf[1] = {0};
  bridge::hal::readFileChunk("test.txt", 0, etl::span<uint8_t>(read_buf, 1));
  bridge::hal::removeFile("test.txt");
  uint8_t dummy_dig, dummy_ana;
  bridge::hal::getPinCounts(dummy_dig, dummy_ana);
  bridge::hal::getCapabilities();
  bridge::hal::getArchId();
  bridge::hal::memory_fence();
  bridge::hal::watchdog_kick();
  bridge::hal::isValidPin(0);
  
  // progmem_compat
  uint8_t pb_addr = 0x12;
  bridge::hal::read_byte(&pb_addr);
  char pb_dest[10];
  bridge::hal::copy_string(pb_dest, "hello", 10);
  bridge::hal::copy_string(nullptr, nullptr, 0);

  // 11. Exhaustive FSM Transitions
  ba.setIdle();
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvStabilized()); 
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
  ba.setIdle(); ba.trigger(bridge::fsm::EvStabilized());
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
  ba.setIdle(); ba.trigger(bridge::fsm::EvStabilized()); ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeComplete());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
  ba.setIdle(); ba.setSynchronized();
  ba.trigger(bridge::fsm::EvSendCritical());
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvAckReceived());
  
  class EvDummy : public etl::message<99> {};
  ba.setIdle(); ba.trigger(EvDummy());
  ba.trigger(bridge::fsm::EvStabilized()); ba.trigger(EvDummy());
  ba.trigger(bridge::fsm::EvHandshakeStart()); ba.trigger(EvDummy());
  ba.trigger(bridge::fsm::EvHandshakeComplete()); ba.trigger(EvDummy());
  ba.trigger(bridge::fsm::EvSendCritical()); ba.trigger(EvDummy());
  ba.trigger(bridge::fsm::EvTimeout()); ba.trigger(EvDummy());

  // Additional Frame coverage
  rpc::Frame f_mem = {}; f_mem.header.command_id = (uint16_t)rpc::CommandId::CMD_GET_FREE_MEMORY; f_mem.header.sequence_id = 5; ba.dispatch(f_mem);
  rpc::Frame f_status_malf = {}; f_status_malf.header.command_id = (uint16_t)rpc::StatusCode::STATUS_MALFORMED; f_status_malf.header.sequence_id = 6; ba.dispatch(f_status_malf);
  rpc::Frame f_status_ack = {}; f_status_ack.header.command_id = (uint16_t)rpc::StatusCode::STATUS_ACK; f_status_ack.header.sequence_id = 7; ba.dispatch(f_status_ack);
  rpc::Frame f_comp = {}; f_comp.header.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION | rpc::RPC_CMD_FLAG_COMPRESSED; f_comp.header.sequence_id = 8; f_comp.payload = etl::span<uint8_t>(); ba.dispatch(f_comp);

  ba.onAckTimeout();
  ba.onRxDedupe();
  ba.onBaudrateChange();
  ba.invokeWatchdog();

  // [SIL-2] Static Timer Callback Coverage
  BridgeClass::onStartupStabilizationTimeout();
  BridgeClass::onBootloaderDelayInternal();
  BridgeClass::onAckTimeoutInternal();
  BridgeClass::onRxDedupeTimeout();
  BridgeClass::onBaudrateChangeTimeout();
  
  class FlowControlStream : public Stream {
  public:
    int avail = 0;
    int available() override { return avail; }
    int read() override { return -1; }
    int peek() override { return -1; }
    size_t write(uint8_t b) override { (void)b; return 1; }
    size_t write(const uint8_t* b, size_t s) override { (void)b; return s; }
    void flush() override {}
  };

  FlowControlStream fc_stream;
  reset_bridge_core(Bridge, fc_stream);
  fc_stream.avail = 100;
  ba.invokeSerialTask(); // Trigger XOFF
  fc_stream.avail = 5;
  ba.invokeSerialTask(); // Trigger XON

  reset_bridge_core(Bridge, stream);

  // [SIL-2] Timer Tick Coverage

  ba.invokeTimerTask(); // first call sets last_tick_ms
  delay(10);
  ba.invokeTimerTask(); // second call triggers tick
  
  // [SIL-2] emitStatus coverage
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, "");
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, F("error message"));
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, (const __FlashStringHelper*)nullptr);

  // [SIL-2] Handshake Tag coverage
  uint8_t nonce[rpc::RPC_HANDSHAKE_NONCE_LENGTH] = {0};
  uint8_t tag[rpc::RPC_HANDSHAKE_TAG_LENGTH] = {0};
  ba.computeHandshakeTag(nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH, tag);

  // SignalXoff/Xon coverage
  Bridge.signalXoff();
  Bridge.signalXon();
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_coverage);
  return UNITY_END();
}
