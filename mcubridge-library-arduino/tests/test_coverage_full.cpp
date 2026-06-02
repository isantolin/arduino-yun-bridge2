#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <etl/array.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "etl_ext/CounterIterator.h"
#include "hal/hal.h"
#include "hal/progmem_compat.h"
#include "protocol/rpc_services.h"
#include "test_support.h"

BridgeClass Bridge(Serial);
// Global stubs for host environment
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;
void setUp(void) {}
void tearDown(void) {}

namespace {
using bridge::test::TestAccessor;

void dummy_datastore_get(etl::string_view k, etl::span<const uint8_t> v) {
  (void)k;
  (void)v;
}
void dummy_fs_read(etl::span<const uint8_t> v) { (void)v; }
void dummy_process_run(int32_t p) { (void)p; }
void dummy_process_poll(rpc::StatusCode s, uint16_t n,
                        etl::span<const uint8_t> st,
                        etl::span<const uint8_t> se) {
  (void)s;
  (void)n;
  (void)st;
  (void)se;
}
void dummy_command_handler(const rpc_pb_RpcEnvelope& f) { (void)f; }

void test_bridge_coverage() {
  printf("Starting test_bridge_coverage...\n");
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Unknown Command
  printf("  - Step 1: Unknown Command\n");
  rpc_pb_RpcEnvelope f_unknown = {};
  f_unknown.command_id = 999;
  ba.dispatch(f_unknown);

  Bridge.onCommand(
      etl::delegate<void(const rpc_pb_RpcEnvelope&)>::create<dummy_command_handler>());
  ba.dispatch(f_unknown);
  Bridge.onCommand(etl::delegate<void(const rpc_pb_RpcEnvelope&)>::create<nullptr>());

  // 2. Duplicate Sequence ID
  printf("  - Step 2: Duplicate Sequence ID\n");
  rpc_pb_RpcEnvelope f_ver = {};
  f_ver.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION;
  f_ver.sequence_id = 1;
  ba.dispatch(f_ver);
  ba.dispatch(f_ver);  // Duplicate

  (void)Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 0, []() {
    rpc::payload::VersionResponse p;
    p.major = 1;
    p.minor = 0;
    p.patch = 0;
    return p;
  }());

  // 3. Pin Handlers
  printf("  - Step 3: Pin Handlers\n");
  rpc_pb_RpcEnvelope f_pin = {};
  f_pin.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_READ;
  bridge::test::set_pb_payload(f_pin, []() {
    rpc::payload::PinRead p;
    p.pin = 255;
    return p;
  }());  // Invalid pin
  ba.dispatch(f_pin);

  f_pin.payload.size = 0;  // Malformed
  ba.dispatch(f_pin);

  rpc_pb_RpcEnvelope f_dw = {};
  f_dw.command_id = (uint16_t)rpc::CommandId::CMD_DIGITAL_WRITE;
  bridge::test::set_pb_payload(f_dw, []() {
    rpc::payload::DigitalWrite p;
    p.pin = 13;
    p.value = 1;
    return p;
  }());
  ba.dispatch(f_dw);

  rpc_pb_RpcEnvelope f_aw = {};
  f_aw.command_id = (uint16_t)rpc::CommandId::CMD_ANALOG_WRITE;
  bridge::test::set_pb_payload(f_aw, []() {
    rpc::payload::AnalogWrite p;
    p.pin = 13;
    p.value = 128;
    return p;
  }());
  ba.dispatch(f_aw);

  rpc_pb_RpcEnvelope f_pm = {};
  f_pm.command_id = (uint16_t)rpc::CommandId::CMD_SET_PIN_MODE;
  bridge::test::set_pb_payload(f_pm, []() {
    rpc::payload::PinMode p;
    p.pin = 13;
    p.mode = 1;
    return p;
  }());
  ba.dispatch(f_pm);

  // 4. Console
  printf("  - Step 4: Console\n");
  
  (void)rpc::services::console::write('a');
  bridge::etl_ext::CounterIterator<int> console_begin(0);
  bridge::etl_ext::CounterIterator<int> console_end(
      bridge::config::CONSOLE_TX_BUFFER_SIZE + 1);
  etl::for_each(console_begin, console_end,
                [](int) { (void)rpc::services::console::write('x'); });
  

  rpc::payload::ConsoleWrite cmsg;
  uint8_t cdata[] = "hello";
  rpc::payload::copy_to_pb_bytes(cmsg.data, cdata, 5);
  ba.invokeConsolePush(cmsg);

  rpc_pb_RpcEnvelope f_cw = {};
  f_cw.command_id = (uint16_t)rpc::CommandId::CMD_CONSOLE_WRITE;
  bridge::test::set_pb_payload(f_cw, cmsg);
  ba.dispatch(f_cw);

  // 5. DataStore
  printf("  - Step 5: DataStore\n");
  uint8_t ds_val[] = {1, 2};
  (void)rpc::services::datastore::put("key", etl::span<const uint8_t>(ds_val, 2));
  rpc::services::datastore::get(
      "key",
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<
          dummy_datastore_get>());
  rpc::payload::DatastoreGetResponse ds_get_p;
  rpc::payload::copy_to_pb_bytes(ds_get_p.value, ds_val, 2);
// //   rpc::services::datastore::_onResponse(ds_get_p);

  rpc_pb_RpcEnvelope f_dsg = {};
  f_dsg.command_id =
      (uint16_t)rpc::CommandId::CMD_DATASTORE_GET_RESP;
  bridge::test::set_pb_payload(f_dsg, ds_get_p);
  ba.dispatch(f_dsg);

  // 6. Mailbox
  printf("  - Step 6: Mailbox\n");
  uint8_t mbox_data[32] = {0};
  (void)rpc::services::mailbox::push(etl::span<const uint8_t>(mbox_data, 3));
  rpc::payload::MailboxPush mpush;
  rpc::payload::copy_to_pb_bytes(mpush.data, mbox_data, 3);
// //   rpc::services::mailbox::_onIncomingData(mpush);

  rpc_pb_RpcEnvelope f_mp = {};
  f_mp.command_id = (uint16_t)rpc::CommandId::CMD_MAILBOX_PUSH;
  bridge::test::set_pb_payload(f_mp, mpush);
  ba.dispatch(f_mp);

  rpc::payload::MailboxReadResponse mread;
  rpc::payload::copy_to_pb_bytes(mread.content, mbox_data, 3);
// //   rpc::services::mailbox::_onIncomingData(mread);

  rpc_pb_RpcEnvelope f_mr = {};
  f_mr.command_id =
      (uint16_t)rpc::CommandId::CMD_MAILBOX_READ_RESP;
  bridge::test::set_pb_payload(f_mr, mread);
  ba.dispatch(f_mr);

  rpc::payload::MailboxAvailableResponse mavl;
  mavl.count = 3;
// //   rpc::services::mailbox::_onAvailableResponse(mavl);

  rpc_pb_RpcEnvelope f_ma = {};
  f_ma.command_id =
      (uint16_t)rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP;
  bridge::test::set_pb_payload(f_ma, mavl);
  ba.dispatch(f_ma);

// //   rpc::services::mailbox::notification(MsgBridgeLost{});
  rpc::services::mailbox::requestRead();
  rpc::services::mailbox::requestAvailable();
  rpc::services::mailbox::signalProcessed();

  // 7. SPI
  printf("  - Step 7: SPI\n");
#if BRIDGE_ENABLE_SPI
  rpc::services::spi::begin();
  rpc::payload::SpiConfig spi_cfg;
  spi_cfg.frequency = 1000000;
  spi_cfg.bit_order = 1;
  spi_cfg.data_mode = 0;
  rpc::services::spi::setConfig(spi_cfg);
  uint8_t spi_buf[2] = {0, 0};
  rpc::services::spi::transfer(etl::span<uint8_t>(spi_buf, 2));
  rpc::services::spi::end();
  rpc::services::spi::transfer(etl::span<uint8_t>(spi_buf, 2));

  rpc_pb_RpcEnvelope f_sc = {};
  f_sc.command_id = (uint16_t)rpc::CommandId::CMD_SPI_SET_CONFIG;
  bridge::test::set_pb_payload(f_sc, spi_cfg);
  ba.dispatch(f_sc);
#endif

  // 8. FileSystem
  printf("  - Step 8: FileSystem\n");
  rpc::services::filesystem::read(
      "test.txt",
      etl::delegate<void(etl::span<const uint8_t>)>::create<dummy_fs_read>());
  rpc::services::filesystem::write("test.txt", etl::span<const uint8_t>(ds_val, 2));
  rpc::services::filesystem::remove("test.txt");

  rpc::payload::FileReadResponse fr_p;
  rpc::payload::copy_to_pb_bytes(fr_p.content, ds_val, 2);
// //   rpc::services::filesystem::_onResponse(fr_p);

  rpc_pb_RpcEnvelope f_fr = {};
  f_fr.command_id = (uint16_t)rpc::CommandId::CMD_FILE_READ_RESP;
  bridge::test::set_pb_payload(f_fr, fr_p);
  ba.dispatch(f_fr);

  rpc_pb_RpcEnvelope f_fw = {};
  f_fw.command_id = (uint16_t)rpc::CommandId::CMD_FILE_WRITE;
  rpc::payload::FileWrite fwp;
  strncpy(fwp.path, "test.txt", sizeof(fwp.path));
  rpc::payload::copy_to_pb_bytes(fwp.data, ds_val, 2);
  bridge::test::set_pb_payload(f_fw, fwp);
  ba.dispatch(f_fw);

  rpc_pb_RpcEnvelope f_flr = {};
  f_flr.command_id = (uint16_t)rpc::CommandId::CMD_FILE_READ;
  rpc::payload::FileRead frp;
  strncpy(frp.path, "test.txt", sizeof(frp.path));
  bridge::test::set_pb_payload(f_flr, frp);
  ba.dispatch(f_flr);

  rpc_pb_RpcEnvelope f_frm = {};
  f_frm.command_id = (uint16_t)rpc::CommandId::CMD_FILE_REMOVE;
  rpc::payload::FileRemove frmp;
  strncpy(frmp.path, "test.txt", sizeof(frmp.path));
  bridge::test::set_pb_payload(f_frm, frmp);
  ba.dispatch(f_frm);

  // 9. Process
  printf("  - Step 9: Process\n");
  rpc::services::process::runAsync(
      "ls", etl::span<const etl::string_view>(),
      etl::delegate<void(int32_t)>::create<dummy_process_run>());
  rpc::services::process::kill(1);
  rpc::services::process::poll(1, etl::delegate<void(
                      rpc::StatusCode, uint16_t, etl::span<const uint8_t>,
                      etl::span<const uint8_t>)>::create<dummy_process_poll>());

  rpc::payload::ProcessKill pk;
  pk.pid = 1;
// //   rpc::services::process::_onKillNotification(pk);

  rpc_pb_RpcEnvelope f_pk = {};
  f_pk.command_id = (uint16_t)rpc::CommandId::CMD_PROCESS_KILL;
  bridge::test::set_pb_payload(f_pk, pk);
  ba.dispatch(f_pk);

  rpc::payload::ProcessRunAsyncResponse prar;
  prar.pid = 123;
// //   rpc::services::process::_onRunAsyncResponse(prar);

  rpc_pb_RpcEnvelope f_prar = {};
  f_prar.command_id =
      (uint16_t)rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP;
  bridge::test::set_pb_payload(f_prar, prar);
  ba.dispatch(f_prar);

  rpc::payload::ProcessPollResponse ppr_p;
  ppr_p.status = 0;
  ppr_p.exit_code = 0;
// //   rpc::services::process::_onPollResponse(ppr_p);

  rpc_pb_RpcEnvelope f_ppr = {};
  f_ppr.command_id =
      (uint16_t)rpc::CommandId::CMD_PROCESS_POLL_RESP;
  bridge::test::set_pb_payload(f_ppr, ppr_p);
  ba.dispatch(f_ppr);

  

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
  rpc_pb_Capabilities caps_dummy = rpc_pb_Capabilities_init_default;
  bridge::hal::fillCapabilities(caps_dummy);
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

  ba.setIdle();
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvSendCritical());
  ba.handleAck(ba.getLastCommandId());

  class EvDummy : public etl::message<99> {};
  ba.trigger(EvDummy());

  ba.onRxDedupe();
  ba.setPendingBaudrate(rpc::RPC_DEFAULT_BAUDRATE);
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
    size_t write(uint8_t b) override {
      (void)b;
      return 1;
    }
    size_t write(const uint8_t* b, size_t s) override {
      (void)b;
      return s;
    }
    void flush() override {}
  };
  FlowStream fs;
  reset_bridge_core(Bridge, fs);
  fs.avail = 100;
  ba.invokeSerialTask();
  fs.avail = 5;
  ba.invokeSerialTask();
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
  etl::fill(lsync.nonce.bytes, lsync.nonce.bytes + 16, 0);
  lsync.nonce.size = 16;
  etl::fill(lsync.tag.bytes, lsync.tag.bytes + 16, 0);
  lsync.tag.size = 16;
  rpc_pb_RpcEnvelope f_ls = {};
  f_ls.command_id = (uint16_t)rpc::CommandId::CMD_LINK_SYNC;
  bridge::test::set_pb_payload(f_ls, lsync);
  ba.dispatch(f_ls);

  rpc_pb_RpcEnvelope f_lr = {};
  f_lr.command_id = (uint16_t)rpc::CommandId::CMD_LINK_RESET;
  bridge::test::set_pb_payload(f_lr, []() {
    rpc::payload::HandshakeConfig p;
    p.ack_timeout_ms = 100;
    p.ack_retry_limit = 3;
    p.response_timeout_ms = 200;
    return p;
  }());
  ba.dispatch(f_lr);

  rpc_pb_RpcEnvelope f_cap = {};
  f_cap.command_id =
      (uint16_t)rpc::CommandId::CMD_GET_CAPABILITIES;
  ba.dispatch(f_cap);

  rpc_pb_RpcEnvelope f_xoff = {};
  f_xoff.command_id = (uint16_t)rpc::CommandId::CMD_XOFF;
  ba.dispatch(f_xoff);

  rpc_pb_RpcEnvelope f_xon = {};
  f_xon.command_id = (uint16_t)rpc::CommandId::CMD_XON;
  ba.dispatch(f_xon);

  rpc_pb_RpcEnvelope f_eb = {};
  f_eb.command_id = (uint16_t)rpc::CommandId::CMD_ENTER_BOOTLOADER;
  bridge::test::set_pb_payload(f_eb, []() {
    rpc::payload::EnterBootloader p;
    p.magic = rpc::RPC_BOOTLOADER_MAGIC;
    return p;
  }());
  ba.dispatch(f_eb);

  rpc_pb_RpcEnvelope f_sb = {};
  f_sb.command_id = (uint16_t)rpc::CommandId::CMD_SET_BAUDRATE;
  bridge::test::set_pb_payload(f_sb, []() {
    rpc::payload::SetBaudratePacket p;
    p.baudrate = 230400;
    return p;
  }());
  ba.dispatch(f_sb);

  printf("Finished test_bridge_coverage.\n");

  // 14. Terminating path
  printf("  - Step 14: Bootloader Delay (triggers exit)\n");
  ba.onBootloaderDelay();
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_coverage);
  return UNITY_END();
}