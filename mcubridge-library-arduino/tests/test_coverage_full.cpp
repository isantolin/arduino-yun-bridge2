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

  // 1. Unknown Command
  rpc::Frame f_unknown = {};
  f_unknown.header.command_id = 999; 
  ba.dispatch(f_unknown);
  
  Bridge.onCommand(etl::delegate<void(const rpc::Frame&)>::create<dummy_command_handler>());
  ba.dispatch(f_unknown);
  Bridge.onCommand(etl::delegate<void(const rpc::Frame&)>::create<nullptr>());

  // 2. Duplicate Sequence ID
  rpc::Frame f_ver = {};
  f_ver.header.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION;
  f_ver.header.sequence_id = ba._last_rx_sequence_id();
  ba.dispatch(f_ver);

  (void)Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 0, rpc::payload::VersionResponse{1,0,0});
  
  // 3. Pin Handlers
  rpc::Frame f_pin = {};
  f_pin.header.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_READ;
  bridge::test::set_pb_payload(f_pin, rpc::payload::PinRead{255}); // Invalid pin
  ba.dispatch(f_pin);

  rpc::Frame f_dw = {}; f_dw.header.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_WRITE;
  bridge::test::set_pb_payload(f_dw, rpc::payload::DigitalWrite{13, 1});
  ba.dispatch(f_dw);

  rpc::Frame f_aw = {}; f_aw.header.command_id = (uint16_t)rpc::CommandId::CMD_ANALOG_WRITE;
  bridge::test::set_pb_payload(f_aw, rpc::payload::AnalogWrite{13, 128});
  ba.dispatch(f_aw);

  rpc::Frame f_pm = {}; f_pm.header.command_id = (uint16_t)rpc::CommandId::CMD_SET_PIN_MODE;
  bridge::test::set_pb_payload(f_pm, rpc::payload::PinMode{13, 1});
  ba.dispatch(f_pm);

  // 4. Console
  Console.begin();
  (void)Console.write('a');
  for(int i=0; i<bridge::config::CONSOLE_TX_BUFFER_SIZE + 1; i++) (void)Console.write('x');
  Console.flush();
  
  rpc::payload::ConsoleWrite cmsg;
  uint8_t cdata[] = "hello";
  cmsg.data = etl::span<const uint8_t>(cdata, 5);
  ba.invokeConsolePush(cmsg);

  // 5. DataStore
  uint8_t ds_val[] = {1, 2};
  (void)DataStore.set("key", etl::span<const uint8_t>(ds_val, 2));
  DataStore.get("key", etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<dummy_datastore_get>());
  rpc::payload::DatastoreGetResponse ds_get;
  ds_get.value = etl::span<const uint8_t>(ds_val, 2);
  DataStore._onResponse(ds_get);

  // 6. Mailbox
  uint8_t mbox_data[bridge::config::MAILBOX_RX_BUFFER_SIZE + 1] = {0};
  (void)Mailbox.push(etl::span<const uint8_t>(mbox_data, 3));
  rpc::payload::MailboxPush mpush;
  mpush.data = etl::span<const uint8_t>(mbox_data, sizeof(mbox_data));
  Mailbox._onIncomingData(mpush);
  rpc::payload::MailboxReadResponse mread;
  mread.content = etl::span<const uint8_t>(mbox_data, sizeof(mbox_data));
  Mailbox._onIncomingData(mread);
  rpc::payload::MailboxAvailableResponse mavl;
  mavl.count = 3;
  Mailbox._onAvailableResponse(mavl);
  Mailbox.notification(MsgBridgeLost{});
  Mailbox.requestRead();
  Mailbox.requestAvailable();
  Mailbox.signalProcessed();

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
  SPIService.end();
  SPIService.transfer(etl::span<uint8_t>(spi_buf, 2)); // Not initialized
#endif

  // 8. FileSystem
  FileSystem.read("test.txt", etl::delegate<void(etl::span<const uint8_t>)>::create<dummy_fs_read>());
  FileSystem.write("test.txt", etl::span<const uint8_t>(ds_val, 2));
  FileSystem.remove("test.txt");
  rpc::payload::FileReadResponse fr;
  fr.content = etl::span<const uint8_t>(ds_val, 2);
  FileSystem._onResponse(fr);
  // Large write
  uint8_t large_data[rpc::MAX_PAYLOAD_SIZE + 10] = {0};
  FileSystem.write("large.txt", etl::span<const uint8_t>(large_data, sizeof(large_data)));

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

  // 10. HAL
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
  
  uint8_t pb_addr = 0x12;
  bridge::hal::read_byte(&pb_addr);
  char pb_dest[10];
  bridge::hal::copy_string(pb_dest, "hello", 10);

  // 11. FSM & Timers
  ba.setIdle();
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvStabilized()); 
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeComplete());
  ba.setIdle(); ba.setSynchronized();
  ba.trigger(bridge::fsm::EvSendCritical());
  ba.trigger(bridge::fsm::EvAckReceived());
  
  class EvDummy : public etl::message<99> {};
  ba.trigger(EvDummy());

  ba.onAckTimeout();
  ba.onRxDedupe();
  ba.onBaudrateChange();
  ba.invokeWatchdog();
  ba.onStartupStabilized();
  ba.onBootloaderDelay();

  // 12. Flow Control
  class FlowStream : public Stream {
  public:
    int avail = 0;
    int available() override { return avail; }
    int read() override { return -1; }
    int peek() override { return -1; }
    size_t write(uint8_t b) override { (void)b; return 1; }
    size_t write(const uint8_t* b, size_t s) override { (void)b; return s; }
    void flush() override {}
  };
  FlowStream fs;
  reset_bridge_core(Bridge, fs);
  fs.avail = 100; ba.invokeSerialTask();
  fs.avail = 5; ba.invokeSerialTask();
  reset_bridge_core(Bridge, stream);

  // 13. Other Status
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, "");
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, F("err"));
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_coverage);
  return UNITY_END();
}
