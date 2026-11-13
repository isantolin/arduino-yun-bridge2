#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE
#endif
#include <Arduino.h>
#include <etl/exception.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <unity.h>

namespace etl {
void handle_error(const etl::exception& e);
}

#include "Bridge.h"
#include "BridgeFaultInjection.h"
#include "BridgeTestInterface.h"
#include "fsm/bridge_fsm.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Process.h"
#include "services/SPIService.h"
#include "test_support.h"

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

using bridge::test::TestAccessor;

void setUp() { bridge::test::fault::reset(); }
void tearDown() {}

namespace {

void on_datastore_get(etl::string_view, pw::span<const uint8_t>) {}
void on_process_run(int32_t) {}
void on_process_poll(rpc::StatusCode, uint16_t, pw::span<const uint8_t>,
                     pw::span<const uint8_t>) {}

inline void feed_rpc_request(TestAccessor& ba, uint32_t service_id,
                             uint32_t method_id,
                             pw::span<const uint8_t> payload = {}) {
  send_hdlc_rpc(ba, service_id, method_id, payload);
}

template <typename ProtoMsg>
inline void feed_rpc_request_msg(TestAccessor& ba, uint32_t service_id,
                                 uint32_t method_id, const pb_msgdesc_t* fields,
                                 const ProtoMsg& msg) {
  send_rpc_request<ProtoMsg, 128>(ba, service_id, method_id, fields, msg);
}

template <typename ProtoMsg>
inline void invoke_response(TestAccessor& ba, uint32_t service_id,
                            uint32_t method_id, const pb_msgdesc_t* fields,
                            const ProtoMsg& msg, uint32_t call_id = 0) {
  invoke_rpc_response(ba, service_id, method_id, fields, msg, call_id);
}

void test_dispatch_valid_payload_handlers_unique_seq() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  constexpr uint32_t kSysSvc = pw::tokenizer::Hash("rpc.pb.SystemService");
  constexpr uint32_t kGpioSvc = pw::tokenizer::Hash("rpc.pb.GpioService");
  constexpr uint32_t kFsSvc = pw::tokenizer::Hash("rpc.pb.FileSystemService");
  constexpr uint32_t kProcSvc = pw::tokenizer::Hash("rpc.pb.ProcessService");
  constexpr uint32_t kSpiSvc = pw::tokenizer::Hash("rpc.pb.SpiService");
  constexpr uint32_t kDsSvc = pw::tokenizer::Hash("rpc.pb.DataStoreService");

  // 1. SetBaudrate
  rpc_pb_SetBaudratePacket baud_msg = rpc_pb_SetBaudratePacket_init_default;
  baud_msg.baudrate = 57600;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("SetBaudrate"),
                       rpc_pb_SetBaudratePacket_fields, baud_msg);

  // 2. SetPinMode
  rpc_pb_PinMode pin_mode_msg = rpc_pb_PinMode_init_default;
  pin_mode_msg.pin = 255;
  pin_mode_msg.mode = rpc_pb_PinModeType_PIN_OUTPUT;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SetPinMode"),
                       rpc_pb_PinMode_fields, pin_mode_msg);

  // 3. DigitalWrite
  rpc_pb_DigitalWrite dwrite_msg = rpc_pb_DigitalWrite_init_default;
  dwrite_msg.pin = 255;
  dwrite_msg.value = 1;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("DigitalWrite"),
                       rpc_pb_DigitalWrite_fields, dwrite_msg);

  // 4. AnalogWrite
  rpc_pb_AnalogWrite awrite_msg = rpc_pb_AnalogWrite_init_default;
  awrite_msg.pin = 255;
  awrite_msg.value = 42;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("AnalogWrite"),
                       rpc_pb_AnalogWrite_fields, awrite_msg);

  // 5. FileWrite
  rpc_pb_FileWrite fw_msg = rpc_pb_FileWrite_init_default;
  strncpy(fw_msg.path, "edge.bin", sizeof(fw_msg.path));
  fw_msg.data.size = 4;
  fw_msg.data.bytes[0] = 1;
  fw_msg.data.bytes[1] = 2;
  fw_msg.data.bytes[2] = 3;
  fw_msg.data.bytes[3] = 4;
  feed_rpc_request_msg(ba, kFsSvc, pw::tokenizer::Hash("Write"),
                       rpc_pb_FileWrite_fields, fw_msg);

  // 7. FileRemove
  rpc_pb_FileRemove frm_msg = rpc_pb_FileRemove_init_default;
  strncpy(frm_msg.path, "edge.bin", sizeof(frm_msg.path));
  feed_rpc_request_msg(ba, kFsSvc, pw::tokenizer::Hash("Remove"),
                       rpc_pb_FileRemove_fields, frm_msg);

  // 8. ProcessKill
  rpc_pb_ProcessKill pk_msg = rpc_pb_ProcessKill_init_default;
  pk_msg.pid = 123;
  feed_rpc_request_msg(ba, kProcSvc, pw::tokenizer::Hash("Kill"),
                       rpc_pb_ProcessKill_fields, pk_msg);

  // 9. SpiConfig
  rpc_pb_SpiConfig sc_msg = rpc_pb_SpiConfig_init_default;
  sc_msg.frequency = 1000000;
  sc_msg.bit_order = rpc_pb_SpiBitOrder_SPI_BIT_ORDER_MSB_FIRST;
  sc_msg.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_0;
  feed_rpc_request_msg(ba, kSpiSvc, pw::tokenizer::Hash("SetConfig"),
                       rpc_pb_SpiConfig_fields, sc_msg);

  // 10. SpiBegin & SpiEnd + Mailbox unimplemented server methods
  constexpr uint32_t kMailboxSvc = pw::tokenizer::Hash("rpc.pb.MailboxService");
  feed_rpc_request(ba, kSpiSvc, pw::tokenizer::Hash("Begin"));
  feed_rpc_request(ba, kSpiSvc, pw::tokenizer::Hash("End"));
  feed_rpc_request(ba, kMailboxSvc, pw::tokenizer::Hash("Read"));
  feed_rpc_request(ba, kMailboxSvc, pw::tokenizer::Hash("Available"));
  feed_rpc_request(ba, kMailboxSvc, pw::tokenizer::Hash("Processed"));

  // 11. GetCapabilities
  feed_rpc_request(ba, kSysSvc, pw::tokenizer::Hash("GetCapabilities"));

  // 12. SpiTransfer
  rpc_pb_SpiTransfer st_msg = rpc_pb_SpiTransfer_init_default;
  feed_rpc_request_msg(ba, kSpiSvc, pw::tokenizer::Hash("Transfer"),
                       rpc_pb_SpiTransfer_fields, st_msg);

  // 13. DataStoreGet response
  DataStore.get("edge_key",
                DataStoreType::GetHandler::create<on_datastore_get>());
  rpc_pb_DatastoreGetResponse ds_resp =
      rpc_pb_DatastoreGetResponse_init_default;
  ds_resp.value.size = 4;
  ds_resp.value.bytes[0] = 1;
  ds_resp.value.bytes[1] = 2;
  ds_resp.value.bytes[2] = 3;
  ds_resp.value.bytes[3] = 4;
  invoke_response(ba, kDsSvc, pw::tokenizer::Hash("Get"),
                  rpc_pb_DatastoreGetResponse_fields, ds_resp);

  // 15. ProcessRunAsync response
  Process.runAsync("ls", pw::span<const etl::string_view>(),
                   ProcessType::ProcessRunHandler::create<on_process_run>());
  rpc_pb_ProcessRunAsyncResponse pr_resp =
      rpc_pb_ProcessRunAsyncResponse_init_default;
  pr_resp.pid = 123;
  invoke_response(ba, kProcSvc, pw::tokenizer::Hash("RunAsync"),
                  rpc_pb_ProcessRunAsyncResponse_fields, pr_resp);

  // 16. ProcessPoll response
  Process.poll(123, ProcessType::ProcessPollHandler::create<on_process_poll>());
  rpc_pb_ProcessPollResponse pp_resp = rpc_pb_ProcessPollResponse_init_default;
  pp_resp.status = 0;
  pp_resp.exit_code = 0;
  pp_resp.stdout_data.size = 4;
  pp_resp.stderr_data.size = 4;
  invoke_response(ba, kProcSvc, pw::tokenizer::Hash("Poll"),
                  rpc_pb_ProcessPollResponse_fields, pp_resp);

  Bridge.process();
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_dispatch_malformed_payload_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  constexpr uint32_t kSysSvc = pw::tokenizer::Hash("rpc.pb.SystemService");
  constexpr uint32_t kGpioSvc = pw::tokenizer::Hash("rpc.pb.GpioService");
  constexpr uint32_t kFsSvc = pw::tokenizer::Hash("rpc.pb.FileSystemService");
  constexpr uint32_t kProcSvc = pw::tokenizer::Hash("rpc.pb.ProcessService");
  constexpr uint32_t kSpiSvc = pw::tokenizer::Hash("rpc.pb.SpiService");
  constexpr uint32_t kClkSvc = pw::tokenizer::Hash("rpc.pb.ClockSyncService");
  constexpr uint32_t kDsSvc = pw::tokenizer::Hash("rpc.pb.DataStoreService");
  constexpr uint32_t kMailboxSvc = pw::tokenizer::Hash("rpc.pb.MailboxService");

  // Malformed payload: truncated or invalid wire bytes
  const uint8_t malformed_bytes[] = {0xC1, 0xFF, 0xEE, 0xDD};
  pw::span<const uint8_t> bad_pl(malformed_bytes, sizeof(malformed_bytes));

  feed_rpc_request(ba, kSysSvc, pw::tokenizer::Hash("SetBaudrate"), bad_pl);
  feed_rpc_request(ba, kSysSvc, pw::tokenizer::Hash("EnterBootloader"), bad_pl);
  feed_rpc_request(ba, kGpioSvc, pw::tokenizer::Hash("SetPinMode"), bad_pl);
  feed_rpc_request(ba, kGpioSvc, pw::tokenizer::Hash("DigitalWrite"), bad_pl);
  feed_rpc_request(ba, kGpioSvc, pw::tokenizer::Hash("AnalogWrite"), bad_pl);
  feed_rpc_request(ba, kGpioSvc, pw::tokenizer::Hash("DigitalRead"), bad_pl);
  feed_rpc_request(ba, kGpioSvc, pw::tokenizer::Hash("AnalogRead"), bad_pl);
  feed_rpc_request(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"), bad_pl);
  feed_rpc_request(ba, kFsSvc, pw::tokenizer::Hash("Write"), bad_pl);
  feed_rpc_request(ba, kFsSvc, pw::tokenizer::Hash("Remove"), bad_pl);
  feed_rpc_request(ba, kProcSvc, pw::tokenizer::Hash("Kill"), bad_pl);
  feed_rpc_request(ba, kSpiSvc, pw::tokenizer::Hash("SetConfig"), bad_pl);
  feed_rpc_request(ba, kSpiSvc, pw::tokenizer::Hash("Transfer"), bad_pl);
  feed_rpc_request(ba, kClkSvc, pw::tokenizer::Hash("ClockSync"), bad_pl);

  // Malformed responses
  DataStore.get("edge_key",
                DataStoreType::GetHandler::create<on_datastore_get>());
  Process.runAsync("ls", pw::span<const etl::string_view>(),
                   ProcessType::ProcessRunHandler::create<on_process_run>());
  Process.poll(123, ProcessType::ProcessPollHandler::create<on_process_poll>());
  pw::rpc::internal::Packet bad_pkt(
      pw::rpc::internal::pwpb::PacketType::RESPONSE, 1, kDsSvc,
      pw::tokenizer::Hash("Get"), 0,
      pw::as_bytes(pw::span(bad_pl.data(), bad_pl.size())));
  ba.invokeRpcResponse(bad_pkt);

  bad_pkt.set_service_id(kMailboxSvc);
  bad_pkt.set_method_id(pw::tokenizer::Hash("Read"));
  ba.invokeRpcResponse(bad_pkt);

  bad_pkt.set_service_id(kMailboxSvc);
  bad_pkt.set_method_id(pw::tokenizer::Hash("Available"));
  ba.invokeRpcResponse(bad_pkt);

  bad_pkt.set_service_id(kFsSvc);
  bad_pkt.set_method_id(pw::tokenizer::Hash("Read"));
  bad_pkt.set_type(pw::rpc::internal::pwpb::PacketType::SERVER_STREAM);
  ba.invokeRpcResponse(bad_pkt);
  bad_pkt.set_type(pw::rpc::internal::pwpb::PacketType::RESPONSE);

  bad_pkt.set_service_id(kProcSvc);
  bad_pkt.set_method_id(pw::tokenizer::Hash("RunAsync"));
  ba.invokeRpcResponse(bad_pkt);

  bad_pkt.set_service_id(kProcSvc);
  bad_pkt.set_method_id(pw::tokenizer::Hash("Poll"));
  ba.invokeRpcResponse(bad_pkt);

  Bridge.process();
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_packet_received_security_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  constexpr uint32_t kGpioSvc = pw::tokenizer::Hash("rpc.pb.GpioService");

  // 1. Valid HDLC frame with DigitalWrite succeeds
  rpc_pb_DigitalWrite dw = rpc_pb_DigitalWrite_init_default;
  dw.pin = 13;
  dw.value = 1;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("DigitalWrite"),
                       rpc_pb_DigitalWrite_fields, dw);
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  TEST_ASSERT_EQUAL_UINT8(HIGH, g_arduino_stub_pin_states[13U].value);

  // 2. Corrupt FCS frame is dropped by decoder
  etl::array<uint8_t, 128> corrupt_frame;
  size_t len =
      build_hdlc_rpc_frame(1, pw::rpc::internal::pwpb::PacketType::REQUEST,
                           kGpioSvc, pw::tokenizer::Hash("DigitalWrite"),
                           pw::span<const uint8_t>(), corrupt_frame);
  TEST_ASSERT_GREATER_THAN(2, len);
  if (len >= 2) {
    corrupt_frame[len - 2] ^= 0xFF;  // Corrupt CRC
  }
  ba.feedHdlcBytes(pw::span<const uint8_t>(corrupt_frame.data(), len));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());

  // 3. Truncated RPC packet inside HDLC frame (< 4 bytes)
  const uint8_t short_bytes[] = {0x01, 0x02};
  etl::array<uint8_t, 64> hdlc_short;
  size_t short_len = TestHdlc::encodeUIFrame(
      1, pw::span<const uint8_t>(short_bytes, sizeof(short_bytes)), hdlc_short);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_short.data(), short_len));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());

  // 4. Unknown HDLC address (e.g. 0x99) dropped safely
  etl::array<uint8_t, 64> hdlc_unknown_addr;
  size_t unk_len = TestHdlc::encodeUIFrame(
      0x99, pw::span<const uint8_t>(short_bytes, sizeof(short_bytes)),
      hdlc_unknown_addr);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_unknown_addr.data(), unk_len));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());

  // 5. Unsynchronized access with shared secret set rejects non-LinkSync RPC
  resetArduinoStubPinStates();
  reset_bridge_core(Bridge, stream, 0U, "top-secret");
  auto& secret_bridge = TestAccessor::create(Bridge);
  secret_bridge.clearSynchronized();

  dw.pin = 14;
  dw.value = 1;
  feed_rpc_request_msg(secret_bridge, kGpioSvc,
                       pw::tokenizer::Hash("DigitalWrite"),
                       rpc_pb_DigitalWrite_fields, dw);
  TEST_ASSERT_FALSE(secret_bridge.isSynchronized());
  TEST_ASSERT_NOT_EQUAL(HIGH, g_arduino_stub_pin_states[14U].value);
}

void test_console_and_policy_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  Console.begin();

  Bridge.enterSafeState();
  TEST_ASSERT_EQUAL(-1, Console.peek());
  TEST_ASSERT_EQUAL(-1, Console.read());

  etl::array<char, bridge::config::CONSOLE_TX_BUFFER_SIZE> fill_chars{};
  etl::fill(fill_chars.begin(), fill_chars.end(), 'x');
  etl::for_each(fill_chars.begin(), fill_chars.end(),
                [](char c) { Console.write(c); });
  TEST_ASSERT_EQUAL_UINT32(0, static_cast<uint32_t>(Console.write('z')));

  // Feed console raw bytes on HDLC Address 0x03
  reset_bridge_core(Bridge, stream);
  auto& ba2 = TestAccessor::create(Bridge);
  ba2.setSynchronized();
  Console.begin();

  const uint8_t console_in[] = {0x41, 0x42};
  etl::array<uint8_t, 64> hdlc_console;
  size_t len = TestHdlc::encodeUIFrame(
      3, pw::span<const uint8_t>(console_in, sizeof(console_in)), hdlc_console);
  ba2.feedHdlcBytes(pw::span<const uint8_t>(hdlc_console.data(), len));

  TEST_ASSERT_EQUAL(0x41, Console.peek());
  TEST_ASSERT_EQUAL(0x41, Console.read());
  TEST_ASSERT_EQUAL(0x42, Console.read());
  TEST_ASSERT_EQUAL(-1, Console.read());

  // Send console/log stream with null stream returns false
  ba2.setNullStream();
  TEST_ASSERT_FALSE(Bridge.sendConsoleStream(
      pw::span<const uint8_t>(console_in, sizeof(console_in))));
  TEST_ASSERT_FALSE(Bridge.sendLogStream(
      pw::span<const uint8_t>(console_in, sizeof(console_in))));

  Bridge.enterSafeState();
  TEST_ASSERT_FALSE(Bridge.sendLogStream(
      pw::span<const uint8_t>(console_in, sizeof(console_in))));
}

void test_observer_and_task_runtime_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.invokeSerialTask();
  ba.invokeTimerTask();

  class FlowStream : public Stream {
   public:
    int avail = 0;
    int available() override { return avail; }
    int read() override { return -1; }
    int peek() override { return -1; }
    size_t write(uint8_t) override { return 1; }
    size_t write(const uint8_t*, size_t s) override { return s; }
    void flush() override {}
  } flow;
  reset_bridge_core(Bridge, flow);
  auto& ba_flow = TestAccessor::create(Bridge);
  flow.avail = bridge::config::FLOW_CONTROL_XOFF_THRESHOLD + 1;
  ba_flow.invokeSerialTask();
  TEST_ASSERT_TRUE(ba_flow.isSerialXoffSent());

  // Keep XOFF latched while the receive queue remains above the XON threshold.
  // This exercises the short-circuit false branch of the XON recovery check.
  ba_flow.invokeSerialTask();
  TEST_ASSERT_TRUE(ba_flow.isSerialXoffSent());

  flow.avail = bridge::config::FLOW_CONTROL_XON_THRESHOLD - 1;
  ba_flow.invokeSerialTask();
  TEST_ASSERT_FALSE(ba_flow.isSerialXoffSent());
  ba_flow.setSynchronized();
  ba_flow.invokeSendFlowControl(true);
  ba_flow.invokeSendFlowControl(false);
  ba_flow.setTxEnabled(false);
  ba_flow.invokeSendFlowControl(true);
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_timer_link_and_bootloader_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream, 0, nullptr);
  auto& ba = TestAccessor::create(Bridge);
  ba.setTimerLastTick(1);
  ba.invokeTimerTask();

  constexpr uint32_t kSysSvc = pw::tokenizer::Hash("rpc.pb.SystemService");

  // LinkSync
  rpc_pb_LinkSync sync = rpc_pb_LinkSync_init_default;
  memset(sync.nonce.bytes, 0x11, 12);
  sync.nonce.size = 12;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("LinkSync"),
                       rpc_pb_LinkSync_fields, sync);

  // LinkReset
  feed_rpc_request(ba, kSysSvc, pw::tokenizer::Hash("LinkReset"));
  TEST_ASSERT_FALSE(Bridge.isSynchronized());

  // SetBaudrate
  rpc_pb_SetBaudratePacket baud_msg = rpc_pb_SetBaudratePacket_init_default;
  baud_msg.baudrate = 115200;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("SetBaudrate"),
                       rpc_pb_SetBaudratePacket_fields, baud_msg);

  // EnterBootloader
  rpc_pb_EnterBootloader boot_msg = rpc_pb_EnterBootloader_init_default;
  boot_msg.magic = rpc::RPC_BOOTLOADER_MAGIC;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("EnterBootloader"),
                       rpc_pb_EnterBootloader_fields, boot_msg);
}

void test_service_capacity_and_send_fail_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  constexpr uint32_t kGpioSvc = pw::tokenizer::Hash("rpc.pb.GpioService");
  rpc_pb_PinMode pm = rpc_pb_PinMode_init_default;
  pm.pin = 13;
  pm.mode = rpc_pb_PinModeType_PIN_OUTPUT;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SetPinMode"),
                       rpc_pb_PinMode_fields, pm);

  Console.begin();
  Console.process();

  Bridge.enterSafeState();
  DataStore.get(
      "key",
      etl::delegate<void(etl::string_view,
                         pw::span<const uint8_t>)>::create<on_datastore_get>());
  Process.poll(123, ProcessType::ProcessPollHandler::create<on_process_poll>());
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_filesystem_spi_fsm_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  constexpr uint32_t kSpiSvc = pw::tokenizer::Hash("rpc.pb.SpiService");

  etl::array<uint8_t, 2> fs_data = {1, 2};
  rpc_pb_FileWrite fwp = rpc_pb_FileWrite_init_default;
  strncpy(fwp.path, "/bad", sizeof(fwp.path));
  fwp.data.size = fs_data.size();
  etl::copy_n(fs_data.data(), fs_data.size(), fwp.data.bytes);
  FileSystem._onWrite(fwp);

  feed_rpc_request(ba, kSpiSvc, pw::tokenizer::Hash("Begin"));

  etl::array<uint8_t, 3> spi_payload = {0xA1, 0xB2, 0xC3};
  rpc_pb_SpiTransfer stp = rpc_pb_SpiTransfer_init_default;
  stp.data.size = spi_payload.size();
  etl::copy_n(spi_payload.data(), spi_payload.size(), stp.data.bytes);
  feed_rpc_request_msg(ba, kSpiSvc, pw::tokenizer::Hash("Transfer"),
                       rpc_pb_SpiTransfer_fields, stp);

  feed_rpc_request(ba, kSpiSvc, pw::tokenizer::Hash("End"));

  bridge::fsm::BridgeFsm fsm;
  fsm.start();
  fsm.receive(bridge::fsm::EvReset());
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_link_sync_authentication_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream, 0U, "shared-key");
  auto& ba = TestAccessor::create(Bridge);

  constexpr uint32_t kSysSvc = pw::tokenizer::Hash("rpc.pb.SystemService");

  // A correctly authenticated LinkSync succeeds with a 16-byte nonce.
  rpc_pb_LinkSync sync = rpc_pb_LinkSync_init_default;
  memset(sync.nonce.bytes, 0xAB, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  sync.nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  ba.computeHandshakeTag(sync.nonce.bytes, sync.nonce.size, sync.tag.bytes);
  sync.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("LinkSync"),
                       rpc_pb_LinkSync_fields, sync);
  TEST_ASSERT_TRUE(ba.isSynchronized());

  // An empty tag is never accepted when a shared secret is configured.
  ba.clearSynchronized();
  sync.tag.size = 0;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("LinkSync"),
                       rpc_pb_LinkSync_fields, sync);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // A non-empty tag is never accepted when no shared secret is configured.
  reset_bridge_core(Bridge, stream, 0U, "");
  sync.tag.size = 1;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("LinkSync"),
                       rpc_pb_LinkSync_fields, sync);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  feed_rpc_request(ba, kSysSvc, pw::tokenizer::Hash("LinkReset"));

  // The explicitly unauthenticated mode requires an empty tag and a valid
  // nonce.
  sync.tag.size = 0;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("LinkSync"),
                       rpc_pb_LinkSync_fields, sync);
  TEST_ASSERT_TRUE(ba.isSynchronized());

  // A short nonce is rejected even when authentication is disabled.
  ba.clearSynchronized();
  sync.nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH - 1U;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("LinkSync"),
                       rpc_pb_LinkSync_fields, sync);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // The firmware self-tests cover SHA-256, HMAC-SHA256, and ChaCha20-Poly1305.
  TEST_ASSERT_TRUE(rpc::security::run_cryptographic_self_tests());
}

void test_fault_injection_harness_paths() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  bridge::test::fault::set_clock_ms(0U);
  SPIService.begin();
  etl::array<uint8_t, 2> spi_buf = {0x10, 0x20};
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);
  TEST_ASSERT_EQUAL_UINT32(
      0,
      static_cast<uint32_t>(SPIService.transfer(pw::span<uint8_t>(spi_buf))));
  SPIService.end();

  TEST_ASSERT_TRUE(rpc::security::run_cryptographic_self_tests());

  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "top-secret");

  bridge::test::fault::reset();
  reset_bridge_core(Bridge, stream);
  auto& ba2 = TestAccessor::create(Bridge);
  ba2.setSynchronized();

  ba2.setHardwareSerial(&Serial);
  ba2.setPendingBaudrate(115200U);
  ba2.onBaudrateChange();

  class FlowStream : public Stream {
   public:
    int avail = 0;
    int available() override { return avail; }
    int read() override { return -1; }
    int peek() override { return -1; }
    size_t write(uint8_t) override { return 1; }
    size_t write(const uint8_t*, size_t s) override { return s; }
    void flush() override {}
  } flow;
  reset_bridge_core(Bridge, flow);
  auto& ba_flow = TestAccessor::create(Bridge);
  ba_flow.setSerialTaskXoffSent(true);
  flow.avail = bridge::config::FLOW_CONTROL_XON_THRESHOLD - 1;
  ba_flow.invokeSerialTask();
}

void test_architectural_extensions_edge_paths() {
  rpc_pb_Capabilities caps = rpc_pb_Capabilities_init_default;
  bridge::hal::fillCapabilities(caps);
  TEST_ASSERT_EQUAL_UINT32(rpc::PROTOCOL_VERSION, caps.ver);
  TEST_ASSERT_TRUE(caps.board_name[0] != '\0');

  // Exercise applySafetyPin all states and default
  bridge::hal::applySafetyPin(10, bridge::hal::SafetyPinState::SAFE_PIN_LOW);
  bridge::hal::applySafetyPin(10, bridge::hal::SafetyPinState::SAFE_PIN_HIGH);
  bridge::hal::applySafetyPin(
      10, bridge::hal::SafetyPinState::SAFE_PIN_INPUT_PULLUP);
  bridge::hal::applySafetyPin(10, bridge::hal::SafetyPinState::SAFE_PIN_INPUT);
  bridge::hal::applySafetyPin(10, static_cast<bridge::hal::SafetyPinState>(99));

  // Safety pins registration and overflow
  Bridge.clearSafetyPins();
  etl::array<uint8_t, 10> pin_range{};
  uint8_t current_pin = 1;
  etl::generate(pin_range.begin(), pin_range.end(),
                [&current_pin]() { return current_pin++; });
  etl::for_each(pin_range.begin(), pin_range.end(), [](uint8_t p) {
    Bridge.registerSafetyPin(p, BridgeClass::SafetyPinState::SAFE_PIN_LOW);
  });
  Bridge.registerSafetyPin(1, BridgeClass::SafetyPinState::SAFE_PIN_HIGH);
  Bridge.enterSafeState();
  Bridge.clearSafetyPins();

  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Watchdog timeout branches
  Bridge.setWatchdogTimeout(100);
  TEST_ASSERT_EQUAL_UINT32(100, Bridge.getWatchdogTimeout());
  ba.setLastRxMs(bridge::test::fault::clock_ms());
  ba.invokeWatchdog();  // (clock_ms() - last_rx_ms) <= 100 -> does NOT expire
  bridge::test::fault::advance_clock_ms(200);
  ba.invokeWatchdog();  // (clock_ms() - last_rx_ms) > 100 -> EXPIRES
  Bridge.setWatchdogTimeout(0);
  TEST_ASSERT_EQUAL_UINT32(0, Bridge.getWatchdogTimeout());

  ba.clearSynchronized();
  Bridge.setWatchdogTimeout(100);
  ba.invokeWatchdog();
  TEST_ASSERT_FALSE(Bridge.isSynchronized());

  Bridge.setWatchdogTimeout(0);
  ba.setSynchronized();
  ba.invokeWatchdog();
  TEST_ASSERT_TRUE(Bridge.isSynchronized());

  constexpr uint32_t kClkSvc = pw::tokenizer::Hash("rpc.pb.ClockSyncService");
  constexpr uint32_t kGpioSvc = pw::tokenizer::Hash("rpc.pb.GpioService");

  // ClockSync
  rpc_pb_ClockSyncRequest sync_req = rpc_pb_ClockSyncRequest_init_default;
  sync_req.host_time_us = 987654321ULL;
  feed_rpc_request_msg(ba, kClkSvc, pw::tokenizer::Hash("ClockSync"),
                       rpc_pb_ClockSyncRequest_fields, sync_req);

  // Disable non-existent pin
  rpc_pb_PinSubscribeRequest sub_dis_unreg =
      rpc_pb_PinSubscribeRequest_init_default;
  sub_dis_unreg.pin = 99;
  sub_dis_unreg.enabled = false;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_dis_unreg);

  // Subscribe pin INPUT
  rpc_pb_PinSubscribeRequest sub_req = rpc_pb_PinSubscribeRequest_init_default;
  sub_req.pin = 2;
  sub_req.mode = rpc_pb_PinModeType_PIN_INPUT;
  sub_req.interval_ms = 10;
  sub_req.hysteresis = 1;
  sub_req.enabled = true;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_req);

  // Subscribe pin INPUT_PULLUP
  rpc_pb_PinSubscribeRequest sub_pu = rpc_pb_PinSubscribeRequest_init_default;
  sub_pu.pin = 3;
  sub_pu.mode = rpc_pb_PinModeType_PIN_INPUT_PULLUP;
  sub_pu.interval_ms = 10;
  sub_pu.hysteresis = 2;
  sub_pu.enabled = true;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_pu);

  // Subscribe pin OUTPUT
  rpc_pb_PinSubscribeRequest sub_an = rpc_pb_PinSubscribeRequest_init_default;
  sub_an.pin = 4;
  sub_an.mode = rpc_pb_PinModeType_PIN_OUTPUT;
  sub_an.interval_ms = 10;
  sub_an.hysteresis = 5;
  sub_an.enabled = true;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_an);

  // Update existing subscription
  sub_req.interval_ms = 5;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_req);

  // Fill subscriptions to full capacity (8 max) then attempt 9th
  etl::array<uint8_t, 8> sub_pins{};
  uint8_t sub_pin_val = 10;
  etl::generate(sub_pins.begin(), sub_pins.end(),
                [&sub_pin_val]() { return sub_pin_val++; });
  etl::for_each(sub_pins.begin(), sub_pins.end(), [&ba, kGpioSvc](uint8_t p) {
    rpc_pb_PinSubscribeRequest s = rpc_pb_PinSubscribeRequest_init_default;
    s.pin = p;
    s.enabled = true;
    feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                         rpc_pb_PinSubscribeRequest_fields, s);
  });
  rpc_pb_PinSubscribeRequest sub_overflow =
      rpc_pb_PinSubscribeRequest_init_default;
  sub_overflow.pin = 50;
  sub_overflow.enabled = true;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_overflow);
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_req);

  // Subscription task execution paths
  ba.clearSynchronized();
  ba.invokeSubscriptionTask();  // !isSynchronized()
  ba.setSynchronized();

  ba.setSubscriptionLastReportMs(0, bridge::test::fault::clock_ms());
  ba.invokeSubscriptionTask();  // now - last_report_ms < interval_ms

  bridge::test::fault::advance_clock_ms(50);
  ba.setSubscriptionLastValue(0, 100);
  ba.invokeSubscriptionTask();  // diff < 0

  ba.setSubscriptionLastValue(0, 0);
  ba.invokeSubscriptionTask();  // diff < hysteresis

  ba.setSubscriptionActive(0, false);
  ba.invokeSubscriptionTask();  // !sub.active

  // Disable subscription
  sub_req.enabled = false;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("SubscribePins"),
                       rpc_pb_PinSubscribeRequest_fields, sub_req);

  // A live synchronized link without an active server-stream writer must
  // reject unsolicited pin events.
  reset_bridge_core(Bridge, stream);
  auto& ba_no_writer = TestAccessor::create(Bridge);
  ba_no_writer.setSynchronized();
  TEST_ASSERT_FALSE(Bridge.sendPinEvent(13, 1));

  // Exercise begin() with baudrate zero: initialization must not restart UART.
  Bridge.begin(0, nullptr);

  // Pin read out of bounds
  rpc_pb_PinRead bad_read = rpc_pb_PinRead_init_default;
  bad_read.pin = 250;
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("DigitalRead"),
                       rpc_pb_PinRead_fields, bad_read);
  feed_rpc_request_msg(ba, kGpioSvc, pw::tokenizer::Hash("AnalogRead"),
                       rpc_pb_PinRead_fields, bad_read);

  // Null stream paths
  ba.setNullStream();
  ba.invokeInitializeRuntime();
  TEST_ASSERT_FALSE(Bridge.sendPinEvent(13, 1));
  reset_bridge_core(Bridge, stream);
  ba.setSynchronized();
}

void test_force_safe_state_uses_core_bus_pin_map() {
  resetArduinoStubPinStates();
  bridge::hal::forceSafeState();

  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[PIN_SPI_SS].mode);
  TEST_ASSERT_EQUAL_UINT8(HIGH, g_arduino_stub_pin_states[PIN_SPI_SS].value);
  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[PIN_SPI_SCK].mode);
  TEST_ASSERT_EQUAL_UINT8(LOW, g_arduino_stub_pin_states[PIN_SPI_SCK].value);
  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[PIN_SPI_MOSI].mode);
  TEST_ASSERT_EQUAL_UINT8(LOW, g_arduino_stub_pin_states[PIN_SPI_MOSI].value);
  TEST_ASSERT_EQUAL_UINT8(INPUT, g_arduino_stub_pin_states[PIN_SPI_MISO].mode);
  TEST_ASSERT_EQUAL_UINT8(INPUT_PULLUP,
                          g_arduino_stub_pin_states[PIN_WIRE_SDA].mode);
  TEST_ASSERT_EQUAL_UINT8(INPUT_PULLUP,
                          g_arduino_stub_pin_states[PIN_WIRE_SCL].mode);

  TEST_ASSERT_EQUAL_UINT8(0xFF, g_arduino_stub_pin_states[0].mode);
  TEST_ASSERT_EQUAL_UINT8(0xFF, g_arduino_stub_pin_states[1].mode);
}

void test_all_timers_and_etl_error_coverage() {
  BiStream stream;
  reset_bridge_core(Bridge, stream, 0, nullptr);
  auto& ba = TestAccessor::create(Bridge);
  ba.setTimerLastTick(1);
  ba.startTimersForCoverage();
  bridge::test::fault::advance_clock_ms(300000);
  ba.invokeTimerTask();

  // Exercise weak etl::handle_error definition
  etl::exception sim_exc("sim_error", "test", 1);
  etl::handle_error(sim_exc);
  TEST_ASSERT_FALSE(ba.isTxEnabled());
}

void test_bootloader_timer_and_hal_coverage() {
  pid_t pid = fork();
  if (pid == 0) {
    BiStream stream;
    reset_bridge_core(Bridge, stream, 0, nullptr);
    auto& ba = TestAccessor::create(Bridge);
    ba.setTimerLastTick(1);
    ba.startBootloaderTimerForCoverage();
    bridge::test::fault::advance_clock_ms(bridge::config::BOOTLOADER_DELAY_MS +
                                          100);
    ba.invokeTimerTask();
    _exit(1);
  }
  int status = 0;
  waitpid(pid, &status, 0);
  TEST_ASSERT_TRUE(WIFEXITED(status));
  TEST_ASSERT_EQUAL_INT(0, WEXITSTATUS(status));
}

template <size_t N>
size_t build_encrypted_hdlc_frame(TestAccessor& ba, uint64_t address,
                                  uint64_t counter,
                                  pw::span<const uint8_t> plaintext,
                                  etl::array<uint8_t, N>& out_hdlc,
                                  bool corrupt_tag = false) {
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, 1U + rpc::RPC_AEAD_COUNTER_SIZE> aad = {};
  etl::array<uint8_t, 256> envelope = {};
  aad[0] = static_cast<uint8_t>(address);
  for (size_t i = 0; i < rpc::RPC_AEAD_COUNTER_SIZE; ++i) {
    const size_t shift = (rpc::RPC_AEAD_COUNTER_SIZE - 1U - i) * 8U;
    const uint8_t b = static_cast<uint8_t>(counter >> shift);
    envelope[i] = b;
    nonce[4U + i] = b;
    aad[1U + i] = b;
  }
  auto ct = pw::span<uint8_t>(envelope.data() + rpc::RPC_AEAD_COUNTER_SIZE,
                              plaintext.size());
  auto tag = pw::span<uint8_t>(
      envelope.data() + rpc::RPC_AEAD_COUNTER_SIZE + plaintext.size(),
      rpc::RPC_AEAD_TAG_SIZE);
  rpc::security::aead_encrypt(ba.rxAeadKey(), nonce, aad, plaintext, ct, tag);
  if (corrupt_tag) {
    tag[0] ^= 0xFFU;
  }
  const size_t env_len =
      rpc::RPC_AEAD_COUNTER_SIZE + plaintext.size() + rpc::RPC_AEAD_TAG_SIZE;
  return TestHdlc::encodeUIFrame(
      address, pw::span<const uint8_t>(envelope.data(), env_len), out_hdlc);
}

void test_encrypted_hdlc_and_session_edge_paths() {
  BiStream stream;
  const char* long_secret =
      "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdefX";
  Bridge.setStream(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, long_secret);
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isTxEnabled());

  reset_bridge_core(Bridge, stream, 0U, "shared-key");
  constexpr uint32_t kSysSvc = pw::tokenizer::Hash("rpc.pb.SystemService");
  rpc_pb_LinkSync sync = rpc_pb_LinkSync_init_default;
  memset(sync.nonce.bytes, 0xCD, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  sync.nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  ba.computeHandshakeTag(sync.nonce.bytes, sync.nonce.size, sync.tag.bytes);
  sync.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
  feed_rpc_request_msg(ba, kSysSvc, pw::tokenizer::Hash("LinkSync"),
                       rpc_pb_LinkSync_fields, sync);
  TEST_ASSERT_TRUE(ba.isSynchronized());
  TEST_ASSERT_TRUE(ba.isSessionKeysReady());

  const uint8_t msg_bytes[] = {'O', 'K'};
  const bool console_sent = Bridge.sendConsoleStream(
      pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)));
  const bool log_sent = Bridge.sendLogStream(
      pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)));
  TEST_ASSERT_TRUE(console_sent);
  TEST_ASSERT_TRUE(log_sent);

  Console.begin();
  etl::array<uint8_t, 256> hdlc_buf = {};
  size_t hdlc_len = build_encrypted_hdlc_frame(
      ba, rpc::RPC_HDLC_ADDRESS_CONSOLE, 1U,
      pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)), hdlc_buf);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), hdlc_len));
  TEST_ASSERT_EQUAL('O', Console.read());
  TEST_ASSERT_EQUAL('K', Console.read());

  hdlc_len = build_encrypted_hdlc_frame(
      ba, rpc::RPC_HDLC_ADDRESS_CONSOLE, 2U,
      pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)), hdlc_buf, true);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), hdlc_len));

  ba.setSessionKeysReady(false);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), hdlc_len));
  ba.setSessionKeysReady(true);

  const uint8_t bad_rpc_inner[] = {0xFF, 0xFF};
  hdlc_len = build_encrypted_hdlc_frame(
      ba, rpc::RPC_HDLC_ADDRESS_RPC, 2U,
      pw::span<const uint8_t>(bad_rpc_inner, sizeof(bad_rpc_inner)), hdlc_buf);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), hdlc_len));

  pw::rpc::internal::Packet sync_pkt(
      pw::rpc::internal::pwpb::PacketType::REQUEST, 1, kSysSvc,
      pw::tokenizer::Hash("LinkSync"), 1, pw::ConstByteSpan());
  uint8_t pkt_buf[64];
  auto enc_sync = sync_pkt.Encode(pw::as_writable_bytes(pw::span(pkt_buf)));
  hdlc_len = build_encrypted_hdlc_frame(
      ba, rpc::RPC_HDLC_ADDRESS_RPC, 3U,
      pw::span<const uint8_t>(pkt_buf, enc_sync.value().size()), hdlc_buf);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), hdlc_len));

  ba.clearSynchronized();
  ba.setSessionKeysReady(true);
  pw::rpc::internal::Packet ver_pkt(
      pw::rpc::internal::pwpb::PacketType::REQUEST, 1, kSysSvc,
      pw::tokenizer::Hash("GetVersion"), 1, pw::ConstByteSpan());
  auto enc_ver = ver_pkt.Encode(pw::as_writable_bytes(pw::span(pkt_buf)));
  hdlc_len = build_encrypted_hdlc_frame(
      ba, rpc::RPC_HDLC_ADDRESS_RPC, 4U,
      pw::span<const uint8_t>(pkt_buf, enc_ver.value().size()), hdlc_buf);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), hdlc_len));
  TEST_ASSERT_FALSE(ba.isSynchronized());

  ba.setSynchronized();
  ba.setSessionKeysReady(true);
  hdlc_len = build_encrypted_hdlc_frame(
      ba, rpc::RPC_HDLC_ADDRESS_RPC, 5U,
      pw::span<const uint8_t>(pkt_buf, enc_ver.value().size()), hdlc_buf);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), hdlc_len));
  feed_rpc_request(ba, kSysSvc, pw::tokenizer::Hash("LinkReset"));
  TEST_ASSERT_FALSE(ba.isSessionKeysReady());

  ba.setSessionKeysReady(true);
  etl::array<uint8_t, 10> small_env = {};
  etl::array<uint8_t, 24> exact_env = {};
  TEST_ASSERT_FALSE(ba.invokeWriteEncryptedHdlcFrameWithBuffer(
      256U, pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)), exact_env));
  TEST_ASSERT_FALSE(ba.invokeWriteEncryptedHdlcFrameWithBuffer(
      1U, pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)), small_env));
  TEST_ASSERT_FALSE(ba.invokeWriteEncryptedHdlcFrameWithBuffer(
      1U, pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)), exact_env));
  ba.setTxAeadCounter(UINT64_MAX);
  etl::array<uint8_t, 64> normal_env = {};
  TEST_ASSERT_FALSE(ba.invokeWriteEncryptedHdlcFrameWithBuffer(
      1U, pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)), normal_env));
  ba.setTxAeadCounter(0U);
  ba.setSessionKeysReady(false);
  TEST_ASSERT_FALSE(ba.invokeWriteEncryptedHdlcFrameWithBuffer(
      1U, pw::span<const uint8_t>(msg_bytes, sizeof(msg_bytes)), normal_env));

  pw::span<const uint8_t> plain_out;
  TEST_ASSERT_FALSE(ba.invokeDecryptHdlcFrame(1U, exact_env, plain_out));
  ba.setSessionKeysReady(true);
  TEST_ASSERT_FALSE(ba.invokeDecryptHdlcFrame(256U, exact_env, plain_out));
  TEST_ASSERT_FALSE(ba.invokeDecryptHdlcFrame(1U, small_env, plain_out));
  etl::array<uint8_t, bridge::config::RX_BUFFER_SIZE + 32U> huge_env = {};
  TEST_ASSERT_FALSE(ba.invokeDecryptHdlcFrame(1U, huge_env, plain_out));
  TEST_ASSERT_FALSE(ba.invokeDecryptHdlcFrame(1U, exact_env, plain_out));
  exact_env[7] = 2U;
  ba.setRxAeadCounter(5U);
  TEST_ASSERT_FALSE(ba.invokeDecryptHdlcFrame(1U, exact_env, plain_out));

  ba.setNullStream();
  const uint8_t dummy_byte = 0x01;
  const pw::Status send_status =
      ba.invokeChannelSend(pw::as_bytes(pw::span(&dummy_byte, 1U)));
  TEST_ASSERT_FALSE(send_status.ok());
}

}  // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_dispatch_valid_payload_handlers_unique_seq);
  RUN_TEST(test_dispatch_malformed_payload_paths);
  RUN_TEST(test_packet_received_security_paths);
  RUN_TEST(test_console_and_policy_edges);
  RUN_TEST(test_observer_and_task_runtime_edges);
  RUN_TEST(test_timer_link_and_bootloader_edges);
  RUN_TEST(test_service_capacity_and_send_fail_edges);
  RUN_TEST(test_filesystem_spi_fsm_edges);
  RUN_TEST(test_link_sync_authentication_paths);
  RUN_TEST(test_fault_injection_harness_paths);
  RUN_TEST(test_architectural_extensions_edge_paths);
  RUN_TEST(test_force_safe_state_uses_core_bus_pin_map);
  RUN_TEST(test_all_timers_and_etl_error_coverage);
  RUN_TEST(test_bootloader_timer_and_hal_coverage);
  RUN_TEST(test_encrypted_hdlc_and_session_edge_paths);
  return UNITY_END();
}
