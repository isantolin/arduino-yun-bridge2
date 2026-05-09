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
  printf("Starting test_bridge_coverage...\n");
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Unknown Command
  printf("  - Step 1: Unknown Command\n");
  rpc::Frame f_unknown = {};
  f_unknown.header.command_id = 999; 
  ba.dispatch(f_unknown);
  
  Bridge.onCommand(etl::delegate<void(const rpc::Frame&)>::create<dummy_command_handler>());
  ba.dispatch(f_unknown);
  Bridge.onCommand(etl::delegate<void(const rpc::Frame&)>::create<nullptr>());

  // 2. Duplicate Sequence ID
  printf("  - Step 2: Duplicate Sequence ID\n");
  rpc::Frame f_ver = {};
  f_ver.header.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION;
  f_ver.header.sequence_id = 1;
  ba.dispatch(f_ver);
  ba.dispatch(f_ver); // Duplicate

  (void)Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 0, rpc::payload::VersionResponse{1,0,0});
  
  // 3. Pin Handlers
  printf("  - Step 3: Pin Handlers\n");
  rpc::Frame f_pin = {};
  f_pin.header.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_READ;
  bridge::test::set_pb_payload(f_pin, rpc::payload::PinRead{255}); // Invalid pin
  ba.dispatch(f_pin);
  
  f_pin.header.payload_length = 0; // Malformed
  ba.dispatch(f_pin);

  rpc::Frame f_dw = {}; f_dw.header.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_WRITE;
  bridge::test::set_pb_payload(f_dw, rpc::payload::DigitalWrite{13, 1});
  ba.dispatch(f_dw);
  ba.dispatch(f_dw); // Duplicate

  rpc::Frame f_aw = {}; f_aw.header.command_id = (uint16_t)rpc::CommandId::CMD_ANALOG_WRITE;
  bridge::test::set_pb_payload(f_aw, rpc::payload::AnalogWrite{13, 128});
  ba.dispatch(f_aw);

  rpc::Frame f_pm = {}; f_pm.header.command_id = (uint16_t)rpc::CommandId::CMD_SET_PIN_MODE;
  bridge::test::set_pb_payload(f_pm, rpc::payload::PinMode{13, 1});
  ba.dispatch(f_pm);

  // 4. Console
  printf("  - Step 4: Console\n");
  Console.begin();
  (void)Console.write('a');
  for(int i=0; i<bridge::config::CONSOLE_TX_BUFFER_SIZE + 1; i++) (void)Console.write('x');
  Console.flush();
  
  rpc::payload::ConsoleWrite cmsg;
  uint8_t cdata[] = "hello";
  cmsg.data = etl::span<const uint8_t>(cdata, 5);
  ba.invokeConsolePush(cmsg);
  
  rpc::Frame f_cw = {}; f_cw.header.command_id = (uint16_t)rpc::CommandId::CMD_CONSOLE_WRITE;
  bridge::test::set_pb_payload(f_cw, cmsg);
  ba.dispatch(f_cw);

  // 5. DataStore
  printf("  - Step 5: DataStore\n");
  uint8_t ds_val[] = {1, 2};
  (void)DataStore.set("key", etl::span<const uint8_t>(ds_val, 2));
  DataStore.get("key", etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<dummy_datastore_get>());
  rpc::payload::DatastoreGetResponse ds_get;
  ds_get.value = etl::span<const uint8_t>(ds_val, 2);
  DataStore._onResponse(ds_get);
  
  rpc::Frame f_dsg = {}; f_dsg.header.command_id = (uint16_t)rpc::CommandId::CMD_DATASTORE_GET_RESP;
  bridge::test::set_pb_payload(f_dsg, ds_get);
  ba.dispatch(f_dsg);

  // 6. Mailbox
  printf("  - Step 6: Mailbox\n");
  uint8_t mbox_data[32] = {0};
  (void)Mailbox.push(etl::span<const uint8_t>(mbox_data, 3));
  rpc::payload::MailboxPush mpush;
  mpush.data = etl::span<const uint8_t>(mbox_data, 3);
  Mailbox._onIncomingData(mpush);
  
  rpc::Frame f_mp = {}; f_mp.header.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_PUSH;
  bridge::test::set_pb_payload(f_mp, mpush);
  ba.dispatch(f_mp);

  rpc::payload::MailboxReadResponse mread;
  mread.content = etl::span<const uint8_t>(mbox_data, 3);
  Mailbox._onIncomingData(mread);
  
  rpc::Frame f_mr = {}; f_mr.header.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_READ_RESP;
  bridge::test::set_pb_payload(f_mr, mread);
  ba.dispatch(f_mr);

  rpc::payload::MailboxAvailableResponse mavl;
  mavl.count = 3;
  Mailbox._onAvailableResponse(mavl);
  
  rpc::Frame f_ma = {}; f_ma.header.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP;
  bridge::test::set_pb_payload(f_ma, mavl);
  ba.dispatch(f_ma);

  Mailbox.notification(MsgBridgeLost{});
  Mailbox.requestRead();
  Mailbox.requestAvailable();
  Mailbox.signalProcessed();

  // 7. SPI
  printf("  - Step 7: SPI\n");
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
  SPIService.transfer(etl::span<uint8_t>(spi_buf, 2));
  
  rpc::Frame f_sc = {}; f_sc.header.command_id = (uint16_t)rpc::CommandId::CMD_SPI_SET_CONFIG;
  bridge::test::set_pb_payload(f_sc, spi_cfg);
  ba.dispatch(f_sc);
#endif

  // 8. FileSystem
  printf("  - Step 8: FileSystem\n");
  FileSystem.read("test.txt", etl::delegate<void(etl::span<const uint8_t>)>::create<dummy_fs_read>());
  FileSystem.write("test.txt", etl::span<const uint8_t>(ds_val, 2));
  FileSystem.remove("test.txt");
  
  rpc::payload::FileReadResponse fr;
  fr.content = etl::span<const uint8_t>(ds_val, 2);
  FileSystem._onResponse(fr);
  
  rpc::Frame f_fr = {}; f_fr.header.command_id = (uint16_t)rpc::CommandId::CMD_FILE_READ_RESP;
  bridge::test::set_pb_payload(f_fr, fr);
  ba.dispatch(f_fr);
  
  rpc::Frame f_fw = {}; f_fw.header.command_id = (uint16_t)rpc::CommandId::CMD_FILE_WRITE;
  rpc::payload::FileWrite fwp; fwp.path = "test.txt"; fwp.data = etl::span<const uint8_t>(ds_val, 2);
  bridge::test::set_pb_payload(f_fw, fwp);
  ba.dispatch(f_fw);

  rpc::Frame f_flr = {}; f_flr.header.command_id = (uint16_t)rpc::CommandId::CMD_FILE_READ;
  rpc::payload::FileRead frp; frp.path = "test.txt";
  bridge::test::set_pb_payload(f_flr, frp);
  ba.dispatch(f_flr);

  rpc::Frame f_frm = {}; f_frm.header.command_id = (uint16_t)rpc::CommandId::CMD_FILE_REMOVE;
  rpc::payload::FileRemove frmp; frmp.path = "test.txt";
  bridge::test::set_pb_payload(f_frm, frmp);
  ba.dispatch(f_frm);

  // 9. Process
  printf("  - Step 9: Process\n");
  ProcessClass::runAsync("ls", etl::span<const etl::string_view>(), etl::delegate<void(int32_t)>::create<dummy_process_run>());
  ProcessClass::kill(1);
  Process.poll(1, etl::delegate<void(rpc::StatusCode, uint8_t, etl::span<const uint8_t>, etl::span<const uint8_t>)>::create<dummy_process_poll>());
  
  rpc::payload::ProcessKill pk;
  pk.pid = 1;
  ProcessClass::_kill(pk);
  
  rpc::Frame f_pk = {}; f_pk.header.command_id = (uint16_t)rpc::CommandId::CMD_PROCESS_KILL;
  bridge::test::set_pb_payload(f_pk, pk);
  ba.dispatch(f_pk);

  rpc::payload::ProcessRunAsyncResponse prar;
  prar.pid = 123;
  ProcessClass::_onRunAsyncResponse(prar);
  
  rpc::Frame f_prar = {}; f_prar.header.command_id = (uint16_t)rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP;
  bridge::test::set_pb_payload(f_prar, prar);
  ba.dispatch(f_prar);

  rpc::payload::ProcessPollResponse ppr;
  ppr.status = 0;
  ppr.exit_code = 0;
  ProcessClass::_onPollResponse(ppr);
  
  rpc::Frame f_ppr = {}; f_ppr.header.command_id = (uint16_t)rpc::CommandId::CMD_PROCESS_POLL_RESP;
  bridge::test::set_pb_payload(f_ppr, ppr);
  ba.dispatch(f_ppr);

  Process.reset();

  // 10. HAL
  printf("  - Step 10: HAL\n");
  bridge::hal::getFreeMemory();
  bridge::hal::init();
  bridge::hal::hasSD();
  bridge::hal::writeFile("test.txt", etl::span<const uint8_t>());
  uint8_t rb[1];
  bridge::hal::readFileChunk("test.txt", 0, etl::span<uint8_t>(rb, 1));
  bridge::hal::removeFile("test.txt");
  uint8_t d, a;
  bridge::hal::getPinCounts(d, a);
  bridge::hal::getCapabilities();
  bridge::hal::getArchId();
  bridge::hal::memory_fence();
  bridge::hal::watchdog_kick();
  bridge::hal::isValidPin(0);
  uint8_t pb_val = 0;
  bridge::hal::read_byte(&pb_val);
  char pd_val[1];
  bridge::hal::copy_string(pd_val, "", 1);

  // 11. FSM & Timers & Retransmission
  printf("  - Step 11: FSM & Timers\n");
  ba.setIdle();
  ba.trigger(bridge::fsm::EvHandshakeFailed());
  ba.trigger(bridge::fsm::EvTimeout());
  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvStabilized()); 
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvHandshakeComplete());
  
  ba.trigger(bridge::fsm::EvSendCritical());
  TEST_ASSERT(ba.isAwaitingAck());
  
  ba.onAckTimeout(); 
  ba.onAckTimeout(); 
  ba.onAckTimeout(); 
  ba.onAckTimeout(); 
  
  ba.setIdle(); ba.setSynchronized();
  ba.trigger(bridge::fsm::EvSendCritical());
  ba.handleAck(ba.getLastCommandId());
  
  class EvDummy : public etl::message<99> {};
  ba.trigger(EvDummy());

  ba.onRxDedupe();
  ba.setPendingBaudrate(115200);
  ba.onBaudrateChange();
  ba.invokeWatchdog();
  
  uint8_t sd_val_x[] = {0};
  stream.feed(sd_val_x, 1);
  ba.onStartupStabilized();

  // 12. Flow Control
  printf("  - Step 12: Flow Control\n");
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

  // 13. Security and Status
  printf("  - Step 13: Security and Status\n");
  ba.clearSynchronized();
  ba.dispatch(f_ver); 
  ba.setSynchronized();
  
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, "");
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, F("err"));
  Bridge.enterSafeState();

  uint8_t rp_val[] = {rpc::PROTOCOL_VERSION, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
  ba.invokePacketReceived(etl::span<const uint8_t>(rp_val, sizeof(rp_val)));
  
  rpc::payload::LinkSync lsync;
  etl::fill(lsync.nonce.begin(), lsync.nonce.end(), 0);
  etl::fill(lsync.tag.begin(), lsync.tag.end(), 0);
  rpc::Frame f_ls = {}; f_ls.header.command_id = (uint16_t)rpc::CommandId::CMD_LINK_SYNC;
  bridge::test::set_pb_payload(f_ls, lsync);
  ba.dispatch(f_ls);

  rpc::Frame f_lr = {}; f_lr.header.command_id = (uint16_t)rpc::CommandId::CMD_LINK_RESET;
  bridge::test::set_pb_payload(f_lr, rpc::payload::HandshakeConfig{100, 3, 200});
  ba.dispatch(f_lr);

  rpc::Frame f_cap = {}; f_cap.header.command_id = (uint16_t)rpc::CommandId::CMD_GET_CAPABILITIES;
  ba.dispatch(f_cap);

  rpc::Frame f_xoff = {}; f_xoff.header.command_id = (uint16_t)rpc::CommandId::CMD_XOFF;
  ba.dispatch(f_xoff);

  rpc::Frame f_xon = {}; f_xon.header.command_id = (uint16_t)rpc::CommandId::CMD_XON;
  ba.dispatch(f_xon);

  rpc::Frame f_eb = {}; f_eb.header.command_id = (uint16_t)rpc::CommandId::CMD_ENTER_BOOTLOADER;
  bridge::test::set_pb_payload(f_eb, rpc::payload::EnterBootloader{rpc::RPC_BOOTLOADER_MAGIC});
  ba.dispatch(f_eb);

  rpc::Frame f_sb = {}; f_sb.header.command_id = (uint16_t)rpc::CommandId::CMD_SET_BAUDRATE;
  bridge::test::set_pb_payload(f_sb, rpc::payload::SetBaudratePacket{230400});
  ba.dispatch(f_sb);

  printf("Finished test_bridge_coverage.\n");
  
  // 14. Terminating path
  printf("  - Step 14: Bootloader Delay (triggers exit)\n");
  ba.onBootloaderDelay();
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_coverage);
  return UNITY_END();
}
