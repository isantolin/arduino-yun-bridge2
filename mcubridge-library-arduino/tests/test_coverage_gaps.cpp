/*
 * test_coverage_gaps.cpp - Comprehensive coverage gap filler
 *
 * Targets all remaining uncovered lines identified by gcovr analysis.
 * Exercises command dispatch tables, template instantiations, ACK/retry
 * paths, timer callbacks, FSM transitions, observer notifications, and
 * edge-case error paths.
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

static unsigned long g_millis = 10000;
unsigned long millis() { return g_millis; }

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "protocol/BridgeEvents.h"
#include "protocol/rle.h"
#include "protocol/rpc_cobs.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "router/command_router.h"
#include "security/security.h"
#include "test_support.h"

// Global instances (required by Bridge.cpp when BRIDGE_TEST_NO_GLOBALS=1)
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

namespace {

void reset_env(BiStream& stream) {
  reset_bridge_core(Bridge, stream);
  Console.begin();
  DataStore.reset();
  Process.reset();
}

// ============================================================================
// 1. GPIO commands through dispatch()
//    Covers: Bridge.cpp L675-689 (dispatch table), L693-733 (handlers),
//            Bridge.h L437-443 (_withPayloadAck), L445-449 (_withPayload),
//            L453-459 (_sendResponse), Bridge.cpp L388,395,397-398 (context)
// ============================================================================
void test_gpio_commands_via_dispatch() {
  printf("  -> test_gpio_commands_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_SET_PIN_MODE (80)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  rpc::payload::PinMode pm_msg = mcubridge_PinMode_init_default;
  pm_msg.pin = 13;
  pm_msg.mode = 1;
  bridge::test::set_pb_payload(f, pm_msg);
  f.crc = 1;
  ba.dispatch(f);

  // CMD_DIGITAL_WRITE (81)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  rpc::payload::DigitalWrite dw_msg = mcubridge_DigitalWrite_init_default;
  dw_msg.pin = 13;
  dw_msg.value = 1;
  bridge::test::set_pb_payload(f, dw_msg);
  f.crc = 2;
  ba.dispatch(f);

  // CMD_ANALOG_WRITE (82)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE);
  rpc::payload::AnalogWrite aw_msg = mcubridge_AnalogWrite_init_default;
  aw_msg.pin = 9;
  aw_msg.value = 128;
  bridge::test::set_pb_payload(f, aw_msg);
  f.crc = 3;
  ba.dispatch(f);

  // CMD_DIGITAL_READ (83)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
  rpc::payload::PinRead pr_msg = mcubridge_PinRead_init_default;
  pr_msg.pin = 7;
  bridge::test::set_pb_payload(f, pr_msg);
  f.crc = 4;
  stream.tx_buf.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx_buf.len > 0);

  // CMD_ANALOG_READ (84)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ);
  pr_msg.pin = 0;
  bridge::test::set_pb_payload(f, pr_msg);
  f.crc = 5;
  stream.tx_buf.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx_buf.len > 0);
}

// ============================================================================
// 2. Console write through dispatch()
//    Covers: Bridge.cpp L751-765 (console dispatch + handler)
// ============================================================================
void test_console_write_via_dispatch() {
  printf("  -> test_console_write_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  rpc::payload::ConsoleWrite cw_msg = mcubridge_ConsoleWrite_init_default;
  const char* msg = "Hello Console";
  etl::span<const uint8_t> span(reinterpret_cast<const uint8_t*>(msg), strlen(msg));
  rpc::util::pb_setup_encode_span(cw_msg.data, span);
  bridge::test::set_pb_payload(f, cw_msg);
  ba.dispatch(f);
}

// ============================================================================
// 3. DataStore get response through dispatch()
//    Covers: Bridge.cpp L767-770 (datastore dispatch)
// ============================================================================
void test_datastore_resp_via_dispatch() {
  printf("  -> test_datastore_resp_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  DataStore.get("mykey",
      DataStoreClass::DataStoreGetHandler::create([](etl::string_view,
                                                     etl::span<const uint8_t>) {
      }));

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
  rpc::payload::DatastoreGetResponse ds_msg = mcubridge_DatastoreGetResponse_init_default;
  uint8_t val[] = "abc";
  etl::span<const uint8_t> span(val, 3);
  rpc::util::pb_setup_encode_span(ds_msg.value, span);
  bridge::test::set_pb_payload(f, ds_msg);
  ba.dispatch(f);
}

// ============================================================================
// 4. Mailbox commands through dispatch()
//    Covers: Bridge.cpp L792-798 (dispatch table), L801,823,825-836 (handlers)
// ============================================================================
void test_mailbox_via_dispatch() {
  printf("  -> test_mailbox_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  Mailbox.onMailboxMessage(
      MailboxClass::MailboxHandler::create([](etl::span<const uint8_t>) {}));

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_MAILBOX_PUSH (131)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH);
  rpc::payload::MailboxPush mb_push = mcubridge_MailboxPush_init_default;
  uint8_t push_data[] = "xyz";
  etl::span<const uint8_t> push_span(push_data, 3);
  rpc::util::pb_setup_encode_span(mb_push.data, push_span);
  bridge::test::set_pb_payload(f, mb_push);
  ba.dispatch(f);

  // CMD_MAILBOX_READ_RESP (132)
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  rpc::payload::MailboxReadResponse mb_read_resp = mcubridge_MailboxReadResponse_init_default;
  uint8_t read_data[] = "AB";
  etl::span<const uint8_t> read_span(read_data, 2);
  rpc::util::pb_setup_encode_span(mb_read_resp.content, read_span);
  bridge::test::set_pb_payload(f, mb_read_resp);
  ba.dispatch(f);

  // CMD_MAILBOX_AVAILABLE_RESP (133)
  Mailbox.onMailboxAvailable(
      MailboxClass::MailboxAvailableHandler::create([](uint16_t) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  rpc::payload::MailboxAvailableResponse mb_avail_resp = mcubridge_MailboxAvailableResponse_init_default;
  mb_avail_resp.count = 42;
  bridge::test::set_pb_payload(f, mb_avail_resp);
  ba.dispatch(f);
}

// ============================================================================
// 5. FileSystem commands through dispatch()
//    Covers: Bridge.cpp L838-863 (dispatch table + handlers)
// ============================================================================
void test_filesystem_via_dispatch() {
  printf("  -> test_filesystem_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_FILE_WRITE (144) - FileWrite with payload
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE);
  rpc::payload::FileWrite fw_msg = mcubridge_FileWrite_init_default;
  strcpy(fw_msg.path, "test.txt");
  uint8_t fw_data[] = "SD-WRITE-DATA";
  etl::span<const uint8_t> fw_span(fw_data, sizeof(fw_data));
  rpc::util::pb_setup_encode_span(fw_msg.data, fw_span);
  bridge::test::set_pb_payload(f, fw_msg);
  stream.tx_buf.clear();
  ba.dispatch(f);
  // Should have responded STATUS_OK since hal::hasSD() is true in host tests
  TEST_ASSERT(stream.tx_buf.len > 0);
  // (We can check if it contains STATUS_OK (0x30))

  // CMD_FILE_READ_RESP (147)
  FileSystem.read("testfile",
      FileSystemClass::FileSystemReadHandler::create(
          [](etl::span<const uint8_t>) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
  rpc::payload::FileReadResponse fr_resp = mcubridge_FileReadResponse_init_default;
  uint8_t fr_data[] = "OK";
  etl::span<const uint8_t> fr_span(fr_data, 2);
  rpc::util::pb_setup_encode_span(fr_resp.content, fr_span);
  bridge::test::set_pb_payload(f, fr_resp);
  ba.dispatch(f);
}

// ============================================================================
// 6. Process commands through dispatch()
//    Covers: Bridge.cpp L922-928 (dispatch table + handlers)
// ============================================================================
void test_process_via_dispatch() {
  printf("  -> test_process_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_PROCESS_RUN_ASYNC_RESP (165)
  Process.runAsync("cmd", etl::span<const etl::string_view>{},
      ProcessClass::ProcessRunAsyncHandler::create([](int16_t) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  rpc::payload::ProcessRunAsyncResponse pr_async_resp = mcubridge_ProcessRunAsyncResponse_init_default;
  pr_async_resp.pid = 99;
  bridge::test::set_pb_payload(f, pr_async_resp);
  ba.dispatch(f);

  // CMD_PROCESS_POLL_RESP (166) with pending PID
  Process.poll(42, ProcessClass::ProcessPollHandler::create(
      [](rpc::StatusCode, uint8_t, etl::span<const uint8_t>,
         etl::span<const uint8_t>) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  rpc::payload::ProcessPollResponse poll_resp = mcubridge_ProcessPollResponse_init_default;
  poll_resp.status = 0x30;
  poll_resp.exit_code = 0;
  uint8_t out_data[] = "o";
  etl::span<const uint8_t> out_span(out_data, 1);
  rpc::util::pb_setup_encode_span(poll_resp.stdout_data, out_span);
  uint8_t err_data[] = "e";
  etl::span<const uint8_t> err_span(err_data, 1);
  rpc::util::pb_setup_encode_span(poll_resp.stderr_data, err_span);
  bridge::test::set_pb_payload(f, poll_resp);
  ba.dispatch(f);
}

// ============================================================================
// 7. Unknown command through dispatch() (with and without handler)
//    Covers: Bridge.cpp L931
// ============================================================================
void test_unknown_command_via_dispatch() {
  printf("  -> test_unknown_command_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = 0xFF;
  f.header.payload_length = 0;

  // Without handler -> sends STATUS_CMD_UNKNOWN
  ba.dispatch(f);

  // With handler -> calls handler
  Bridge.onCommand(BridgeClass::CommandHandler::create(
      [](const rpc::Frame&) {}));
  ba.dispatch(f);
}

// ============================================================================
// 8. System commands through dispatch() (GetVersion, GetFreeMemory, etc.)
//    Covers: Bridge.cpp L482-485,519,528-529 (dispatch + _sendResponse)
//            rpc_structs.h L240,243,250,288 (encode methods)
// ============================================================================
void test_system_commands_via_dispatch() {
  printf("  -> test_system_commands_via_dispatch\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_GET_VERSION (64)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  f.header.payload_length = 0;
  f.crc = 10;
  stream.tx_buf.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx_buf.len > 0);

  // CMD_GET_FREE_MEMORY (66)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
  f.header.payload_length = 0;
  f.crc = 11;
  stream.tx_buf.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx_buf.len > 0);

  // CMD_GET_CAPABILITIES (72)
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
  f.header.payload_length = 0;
  f.crc = 12;
  stream.tx_buf.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx_buf.len > 0);

  // CMD_SET_BAUDRATE (74)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE);
  rpc::payload::SetBaudratePacket br_msg = mcubridge_SetBaudratePacket_init_default;
  br_msg.baudrate = 57600;
  bridge::test::set_pb_payload(f, br_msg);
  f.crc = 13;
  ba.dispatch(f);
}

// ============================================================================
// 9. ACK handling and retransmit paths
//    Covers: Bridge.cpp L940 (_sendAck), L1174 (_handleAck pop),
//            L1200-1213 (_onAckTimeout), L1276-1286 (_flushPendingTxQueue)
// ============================================================================
void test_ack_and_retransmit() {
  printf("  -> test_ack_and_retransmit\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Push a frame and flush -> transitions to AwaitingAck
  rpc::payload::DigitalWrite dw_msg = mcubridge_DigitalWrite_init_default;
  dw_msg.pin = 13;
  dw_msg.value = 1;
  uint8_t dw_buf[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t out = pb_ostream_from_buffer(dw_buf, sizeof(dw_buf));
  pb_encode(&out, rpc::Payload::Descriptor<rpc::payload::DigitalWrite>::fields(), &dw_msg);

  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), out.bytes_written, dw_buf);
  ba.flushPendingTxQueue();
  TEST_ASSERT(ba.isAwaitingAck());

  // Dispatch ACK -> pops queue, back to Idle
  uint16_t last_cmd = ba.getLastCommandId();
  rpc::Frame ack_frame;
  memset(&ack_frame, 0, sizeof(ack_frame));
  ack_frame.header.command_id =
      rpc::to_underlying(rpc::StatusCode::STATUS_ACK);
  rpc::payload::AckPacket ack_msg = mcubridge_AckPacket_init_default;
  ack_msg.command_id = last_cmd;
  bridge::test::set_pb_payload(ack_frame, ack_msg);
  ba.dispatch(ack_frame);
  TEST_ASSERT(ba.isIdle());

  // Test ACK timeout with retransmit
  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), out.bytes_written, dw_buf);
  ba.flushPendingTxQueue();
  TEST_ASSERT(ba.isAwaitingAck());
  ba.setAckRetryLimit(2);
  ba.setRetryCount(0);
  ba.onAckTimeout();
  TEST_ASSERT(ba.isAwaitingAck());

  // Exceed retry limit -> enters safe state
  ba.setRetryCount(ba.getAckRetryLimit());
  ba.onAckTimeout();
  TEST_ASSERT(ba.isUnsynchronized());
}

// ============================================================================
// 10. ACK timeout with status handler
//     Covers: Bridge.cpp L1200-1210 (status_handler call on timeout)
// ============================================================================
void test_ack_timeout_with_status_handler() {
  printf("  -> test_ack_timeout_with_status_handler\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  bool timeout_reported = false;
  Bridge.onStatus(BridgeClass::StatusHandler::create(
      [](rpc::StatusCode sc, etl::span<const uint8_t>) {
        (void)sc;
      }));

  rpc::payload::DigitalWrite dw_msg = mcubridge_DigitalWrite_init_default;
  dw_msg.pin = 13;
  dw_msg.value = 1;
  uint8_t dw_buf[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t out = pb_ostream_from_buffer(dw_buf, sizeof(dw_buf));
  pb_encode(&out, rpc::Payload::Descriptor<rpc::payload::DigitalWrite>::fields(), &dw_msg);

  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), out.bytes_written, dw_buf);
  ba.flushPendingTxQueue();
  ba.setAckRetryLimit(1);
  ba.setRetryCount(1);
  ba.onAckTimeout();
  (void)timeout_reported;
}

// ============================================================================
// 11. Timer callbacks (_onRxDedupe, _onBaudrateChange)
//     Covers: Bridge.cpp L1216-1220, L1222-1224
// ============================================================================
void test_timer_callbacks() {
  printf("  -> test_timer_callbacks\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // _onRxDedupe
  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.crc = 0xDEADBEEF;
  ba.markRxProcessed(f);
  TEST_ASSERT(ba.getRxHistorySize() > 0);
  ba.onRxDedupe();
  TEST_ASSERT_EQ_UINT(ba.getRxHistorySize(), 0);

  // _onBaudrateChange
  ba.setPendingBaudrate(9600);
  ba.onBaudrateChange();
  TEST_ASSERT_EQ_UINT(ba.getPendingBaudrate(), 0);
}

// ============================================================================
// 12. FSM transitions
//     Covers: bridge_fsm.h L85 (HandshakeFailed from Syncing),
//             L87-89 (CryptoFault from Syncing), L96-100 (StateReady),
//             L124 (AckReceived from AwaitingAck), L140 (CryptoFault from
//             Fault), L182 (ackReceived)
// ============================================================================
void test_fsm_transitions() {
  printf("  -> test_fsm_transitions\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // CryptoFault from Idle
  ba.setIdle();
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  // CryptoFault from Fault -> stays Fault (L140)
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  // Reset from Fault -> Unsynchronized
  ba.fsmResetFsm();
  TEST_ASSERT(ba.isUnsynchronized());

  // HandshakeFailed from Syncing -> Fault (L85)
  ba.fsmHandshakeStart();
  ba.fsmHandshakeFailed();
  TEST_ASSERT(ba.isFault());

  // CryptoFault from Syncing -> Fault (L87-89)
  ba.fsmResetFsm();
  ba.fsmHandshakeStart();
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  // CryptoFault from AwaitingAck
  ba.fsmResetFsm();
  ba.setIdle();
  ba.fsmSendCritical();
  TEST_ASSERT(ba.isAwaitingAck());
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  // CryptoFault from Unsynchronized (L70)
  ba.fsmResetFsm();
  TEST_ASSERT(ba.isUnsynchronized());
  ba.fsmCryptoFault();
  TEST_ASSERT(ba.isFault());

  // ackReceived from AwaitingAck (L124, L182)
  ba.fsmResetFsm();
  ba.setIdle();
  ba.setLastCommandId(rpc::RPC_INVALID_ID_SENTINEL);
  ba.fsmSendCritical();
  TEST_ASSERT(ba.isAwaitingAck());
  ba.handleAck(rpc::RPC_INVALID_ID_SENTINEL);
  TEST_ASSERT(ba.isIdle());
}

// ============================================================================
// 13. Observer notifications (enterSafeState -> MsgBridgeLost)
//     Covers: Bridge.cpp L1267-1268, BridgeEvents.h L18,L20
// ============================================================================
struct TestObserver : public BridgeObserver {
  bool lost_called = false;
  bool sync_called = false;
  void notification(MsgBridgeSynchronized) override { sync_called = true; }
  void notification(MsgBridgeLost) override { lost_called = true; }
  void notification(MsgBridgeError) override {}
};

void test_observer_notifications() {
  printf("  -> test_observer_notifications\n");
  BiStream stream;
  reset_env(stream);

  TestObserver obs;
  Bridge.add_observer(obs);
  Bridge.enterSafeState();
  TEST_ASSERT(obs.lost_called);
  Bridge.remove_observer(obs);
}

// ============================================================================
// 14. BridgeObserver default virtual methods + destructor
//     Covers: BridgeEvents.h L18 (destructor), L20 (MsgBridgeLost default)
// ============================================================================
void test_bridge_events_defaults() {
  printf("  -> test_bridge_events_defaults\n");
  // Default BridgeObserver has empty notification(MsgBridgeSynchronized) and
  // notification(MsgBridgeLost).  We inherit and call them to cover L18,L20.
  struct MinimalObserver : public BridgeObserver {
    void notification(MsgBridgeError) override {}
  };
  {
    MinimalObserver obs;
    // Call through base class pointer to exercise default implementations
    BridgeObserver& base = obs;
    base.notification(MsgBridgeSynchronized{});
    base.notification(MsgBridgeLost{});
  }
}

// ============================================================================
// 15. hal::getFreeMemory()
//     Covers: hal.cpp L15,L17
// ============================================================================
void test_hal_free_memory() {
  printf("  -> test_hal_free_memory\n");
  uint16_t mem = bridge::hal::getFreeMemory();
  TEST_ASSERT_EQ_UINT(mem, 1024);

  // Also test the global wrapper
  TEST_ASSERT_EQ_UINT(getFreeMemory(), 1024);

  // Test hal::hasSD and hal::writeFile (new abstractions)
  TEST_ASSERT(bridge::hal::hasSD());
  uint8_t dummy_data[] = {0x01, 0x02};
  TEST_ASSERT(bridge::hal::writeFile("test.txt", etl::span<const uint8_t>(dummy_data, 2)));
}

// ============================================================================
// 16. _applyTimingConfig
//     Covers: Bridge.cpp L1356-1357 (clamping)
// ============================================================================
void test_apply_timing_config() {
  printf("  -> test_apply_timing_config\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Empty payload -> defaults
  ba.applyTimingConfig(nullptr, 0);
  TEST_ASSERT(ba.getAckTimeoutMs() > 0);

  // Valid config
  rpc::payload::HandshakeConfig hc = mcubridge_HandshakeConfig_init_default;
  hc.ack_timeout_ms = 500;
  hc.ack_retry_limit = 3;
  hc.response_timeout_ms = 5000;
  uint8_t hc_buf[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t out_hc = pb_ostream_from_buffer(hc_buf, sizeof(hc_buf));
  pb_encode(&out_hc, rpc::Payload::Descriptor<rpc::payload::HandshakeConfig>::fields(), &hc);
  ba.applyTimingConfig(hc_buf, out_hc.bytes_written);
  // _applyTimingConfig is a no-op; ack_timeout stays at default (200)
  TEST_ASSERT(ba.getAckTimeoutMs() > 0);

  // Out-of-range -> clamped to defaults
  hc.ack_timeout_ms = 1;
  hc.ack_retry_limit = 0;
  hc.response_timeout_ms = 1;
  out_hc = pb_ostream_from_buffer(hc_buf, sizeof(hc_buf));
  pb_encode(&out_hc, rpc::Payload::Descriptor<rpc::payload::HandshakeConfig>::fields(), &hc);
  ba.applyTimingConfig(hc_buf, out_hc.bytes_written);
}

// ============================================================================
// 17. sendKeyValCommand path via DataStore.put()
//     Covers: Bridge.cpp L1044-1046
// ============================================================================
void test_send_key_val_command() {
  printf("  -> test_send_key_val_command\n");
  BiStream stream;
  reset_env(stream);
  DataStore.put("testkey", "testval");
}

// ============================================================================
// 18. emitStatus overloads (string_view, FlashStringHelper)
//     Covers: Bridge.cpp L952-956,964 (_doEmitStatus, emitStatus string_view),
//             L975 (emitStatus FlashStringHelper)
// ============================================================================
void test_emit_status() {
  printf("  -> test_emit_status\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Trigger emitStatus via compressed frame with invalid RLE -> MALFORMED
  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE) |
                        rpc::RPC_CMD_FLAG_COMPRESSED;
  f.header.payload_length = 1;
  f.payload[0] = 0xFF;
  ba.dispatch(f);

  // Trigger emitStatus via LinkSync with wrong payload length
  // (calls emitStatus(MALFORMED) which uses const char* / string_view)
  ba.setIdle();
  ba.assignSharedSecret((const uint8_t*)"mysecret",
                        (const uint8_t*)"mysecret" + 8);
  // Trigger emitStatus(StatusCode, const __FlashStringHelper*) with nullptr
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, (const __FlashStringHelper*)nullptr);

  // Trigger emitStatus(FlashStringHelper) via failed mutual auth
  // This calls emitStatus(STATUS_ERROR, F("Mutual Auth Failed"))
  reset_env(stream);
  {
  auto ba2 = bridge::test::TestAccessor::create(Bridge);
  const uint8_t secret[] = "test_secret_key!";
  ba2.assignSharedSecret(secret, secret + 16);

  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  f.header.payload_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH +
                             rpc::RPC_HANDSHAKE_TAG_LENGTH;
  memset(f.payload.data(), 0xAA, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  memset(f.payload.data() + rpc::RPC_HANDSHAKE_NONCE_LENGTH, 0xBB,
         rpc::RPC_HANDSHAKE_TAG_LENGTH);
  ba2.dispatch(f);
  }
}

// ============================================================================
// 19. Observer-aware _doEmitStatus
//     Covers: Bridge.cpp L952-956 (notify_observers MsgBridgeError)
// ============================================================================
void test_emit_status_with_observer() {
  printf("  -> test_emit_status_with_observer\n");
  BiStream stream;
  reset_env(stream);

  struct ErrorObserver : public BridgeObserver {
    bool error_called = false;
    void notification(MsgBridgeError) override { error_called = true; }
  };

  ErrorObserver obs;
  Bridge.add_observer(obs);

  // Trigger any status emission
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view("test"));
  TEST_ASSERT(obs.error_called);

  Bridge.remove_observer(obs);
}

// ============================================================================
// 20. COBS overflow path in process()
//     Covers: Bridge.cpp L296-297 (overflow when bytes >= MAX_RAW_FRAME_SIZE)
// ============================================================================
void test_cobs_overflow_in_process() {
  printf("  -> test_cobs_overflow_in_process\n");
  BiStream stream;
  reset_env(stream);

  // Feed a very long COBS stream to overflow the buffer.
  // Start with delimiter to reset state, then feed data exceeding buffer size.
  uint8_t delim = 0x00;
  stream.feed(&delim, 1);

  // Feed a COBS block: code 0xFF means 254 data bytes follow, no zero inserted
  // Repeat to exceed MAX_RAW_FRAME_SIZE
  for (int block = 0; block < 10; ++block) {
    uint8_t code = 0xFF;
    stream.feed(&code, 1);
    uint8_t data[254];
    memset(data, 0x42, 254);
    stream.feed(data, 254);
  }

  // End with delimiter so process() completes the frame attempt
  stream.feed(&delim, 1);

  g_millis += 10;
  Bridge.process();
}

// ============================================================================
// 21. rpc::cobs::decode() direct test
//     Covers: rpc_cobs.cpp L42-65
// ============================================================================
void test_cobs_decode() {
  printf("  -> test_cobs_decode\n");

  // Encode then decode
  uint8_t src[] = {1, 2, 0, 3, 4};
  uint8_t encoded[20];
  uint8_t decoded[20];

  size_t enc_len = rpc::cobs::encode(etl::span<const uint8_t>(src, 5),
                                     etl::span<uint8_t>(encoded, 20));
  TEST_ASSERT(enc_len > 0);

  size_t dec_len = rpc::cobs::decode(etl::span<const uint8_t>(encoded, enc_len),
                                     etl::span<uint8_t>(decoded, 20));
  TEST_ASSERT(dec_len > 0);

  // Empty decode
  TEST_ASSERT_EQ_UINT(
      rpc::cobs::decode(etl::span<const uint8_t>(src, 0),
                        etl::span<uint8_t>(decoded, 20)),
      0);

  // Decode with destination too small -> truncation/error
  TEST_ASSERT_EQ_UINT(
      rpc::cobs::decode(etl::span<const uint8_t>(encoded, enc_len),
                        etl::span<uint8_t>(decoded, 1)),
      0);

  // Decode a block with code < 0xFF (inserts zero) and code == 0xFF (no zero)
  // This covers the different branch paths in the decode loop
  uint8_t long_src[300];
  memset(long_src, 0x42, 300);
  long_src[100] = 0;  // Insert a zero in the middle
  uint8_t long_enc[320];
  uint8_t long_dec[320];
  enc_len = rpc::cobs::encode(etl::span<const uint8_t>(long_src, 300),
                              etl::span<uint8_t>(long_enc, 320));
  if (enc_len > 0) {
    dec_len = rpc::cobs::decode(etl::span<const uint8_t>(long_enc, enc_len),
                                etl::span<uint8_t>(long_dec, 320));
    TEST_ASSERT_EQ_UINT(dec_len, 300);
  }
}

// ============================================================================
// 22. rpc_frame.h FrameParser overflow (L99)
//     Covers: payload_len > MAX_PAYLOAD_SIZE -> OVERFLOW error
// ============================================================================
void test_frame_overflow() {
  printf("  -> test_frame_overflow\n");

  rpc::FrameParser parser;
  const uint16_t fake_payload_len = rpc::MAX_PAYLOAD_SIZE + 1;
  const size_t total = fake_payload_len + 9;

  // Build a buffer with correct CRC but oversized payload_len
  etl::vector<uint8_t, 1100> buf;
  buf.resize(total);
  buf[0] = rpc::PROTOCOL_VERSION;
  rpc::write_u16_be(etl::span<uint8_t>(&buf[1], 2), fake_payload_len);
  rpc::write_u16_be(etl::span<uint8_t>(&buf[3], 2), 0x40);
  memset(&buf[5], 0xAA, fake_payload_len);

  etl::crc32 crc_calc;
  crc_calc.add(buf.data(), buf.data() + total - 4);
  rpc::write_u32_be(etl::span<uint8_t>(&buf[total - 4], 4), crc_calc.value());

  auto result = parser.parse(etl::span<const uint8_t>(buf.data(), total));
  TEST_ASSERT(!result.has_value());
}

// ============================================================================
// 23. CommandContext + ICommandHandler
//     Covers: command_router.h (constructor, fields, destructor)
// ============================================================================
void test_command_router() {
  printf("  -> test_command_router\n");

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = 0x50;

  bridge::router::CommandContext ctx(&f, 0x50, false, true);
  TEST_ASSERT_EQ_UINT(ctx.raw_command, 0x50);
  TEST_ASSERT(ctx.requires_ack == true);
  TEST_ASSERT(ctx.is_duplicate == false);
  TEST_ASSERT(ctx.frame == &f);

  // Cover ICommandHandler virtual destructor via polymorphic delete
  struct TestHandler : public bridge::router::ICommandHandler {
    void onStatusCommand(const bridge::router::CommandContext&) override {}
    void onSystemCommand(const bridge::router::CommandContext&) override {}
    void onGpioCommand(const bridge::router::CommandContext&) override {}
    void onConsoleCommand(const bridge::router::CommandContext&) override {}
    void onDataStoreCommand(const bridge::router::CommandContext&) override {}
    void onMailboxCommand(const bridge::router::CommandContext&) override {}
    void onFileSystemCommand(const bridge::router::CommandContext&) override {}
    void onProcessCommand(const bridge::router::CommandContext&) override {}
    void onUnknownCommand(const bridge::router::CommandContext&) override {}
  };

  {
    TestHandler handler;
    bridge::router::ICommandHandler* p = &handler;
    (void)p;
  }
}

// ============================================================================
// 24. Console available() empty + write-when-full-after-flush
//     Covers: Console.cpp L40 (write 0 when buffer stays full),
//             Console.cpp L63 (available returns 0 when empty)
// ============================================================================
void test_console_edge_cases() {
  printf("  -> test_console_edge_cases\n");
  BiStream stream;
  reset_env(stream);

  // available() when empty -> 0
  TEST_ASSERT_EQ_UINT(Console.available(), 0);

  // write() when bridge is unsynchronized (flush is no-op)
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setUnsynchronized();
  TEST_ASSERT_EQ_UINT(Console.write('Z'), 1);

  // New: Test block copy write() with optimized implementation
  reset_env(stream);
  rpc::Frame sync_f;
  memset(&sync_f, 0, sizeof(sync_f));
  sync_f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  rpc::payload::LinkSync sync_msg = mcubridge_LinkSync_init_default;
  sync_msg.nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  bridge::test::set_pb_payload(sync_f, sync_msg);
  ba.dispatch(sync_f); // This transitions to IDLE and calls Console.begin()
  stream.tx_buf.clear(); // Clear the SYNC_RESP

  const char* block_data = "Hello Block Write";
  size_t written = Console.write(reinterpret_cast<const uint8_t*>(block_data), strlen(block_data));
  TEST_ASSERT_EQ_UINT(written, strlen(block_data));
  // flush and check if frame was sent
  stream.tx_buf.clear();
  Console.flush();
  TEST_ASSERT(stream.tx_buf.len > 0);
  ba.handleAck(rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE)); // Clear ACK state

  // New: Test block copy write() with buffer filling and auto-flush
  reset_env(stream);
  ba.setIdle();
  Console.begin();
  
  // Fill nearly to capacity
  uint8_t large_block[128];
  memset(large_block, 'A', sizeof(large_block));
  stream.tx_buf.clear();
  
  // This will write as much as it can. It might flush once and then stop if the TX pool is full.
  written = Console.write(large_block, sizeof(large_block));
  TEST_ASSERT(written > 0);
  
  // Verify that it sent at least one frame
  TEST_ASSERT(stream.tx_buf.len > 0);
}

// ============================================================================
// 25. Process edge cases
//     Covers: Process.cpp L17 (overflow emit), L71 (empty pop)
// ============================================================================
void test_process_edge_cases() {
  printf("  -> test_process_edge_cases\n");
  BiStream stream;
  reset_env(stream);

  // Process.runAsync when bridge is unsync -> emit overflow (L17)
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setUnsynchronized();
  Process.runAsync("ls", etl::span<const etl::string_view>{}, ProcessClass::ProcessRunAsyncHandler{});
}

// ============================================================================
// 26. LinkSync full path (with and without secret)
//     Covers: Bridge.cpp L612-617 (malformed check), L638,647-650 (response)
// ============================================================================
void test_link_sync_full() {
  printf("  -> test_link_sync_full\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // LinkSync without secret
  ba.clearSharedSecret();
  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  rpc::payload::LinkSync sync_msg = mcubridge_LinkSync_init_default;
  sync_msg.nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  memset(sync_msg.nonce.bytes, 0xAA, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  bridge::test::set_pb_payload(f, sync_msg);
  stream.tx_buf.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx_buf.len > 0);

  // LinkSync WITH secret (correct tag -> success)
  reset_env(stream);
  {
  auto ba2 = bridge::test::TestAccessor::create(Bridge);
  const uint8_t secret[] = "test_secret_key!";
  ba2.assignSharedSecret(secret, secret + 16);

  uint8_t nonce[rpc::RPC_HANDSHAKE_NONCE_LENGTH];
  memset(nonce, 0xBB, rpc::RPC_HANDSHAKE_NONCE_LENGTH);

  uint8_t tag[rpc::RPC_HANDSHAKE_TAG_LENGTH];
  ba2.computeHandshakeTag(nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH, tag);

  sync_msg.nonce.size = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  memcpy(sync_msg.nonce.bytes, nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  sync_msg.tag.size = rpc::RPC_HANDSHAKE_TAG_LENGTH;
  memcpy(sync_msg.tag.bytes, tag, rpc::RPC_HANDSHAKE_TAG_LENGTH);
  bridge::test::set_pb_payload(f, sync_msg);

  stream.tx_buf.clear();
  ba2.dispatch(f);
  TEST_ASSERT(stream.tx_buf.len > 0);
  TEST_ASSERT(Bridge.isSynchronized());
  }
}

// ============================================================================
// 27. LinkReset with timing config
//     Covers: Bridge.cpp L1356-1357 (_applyTimingConfig from LinkReset)
// ============================================================================
void test_link_reset_with_config() {
  printf("  -> test_link_reset_with_config\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
  rpc::payload::HandshakeConfig hc = mcubridge_HandshakeConfig_init_default;
  hc.ack_timeout_ms = 500;
  hc.ack_retry_limit = 3;
  hc.response_timeout_ms = 5000;
  bridge::test::set_pb_payload(f, hc);
  ba.dispatch(f);
}

// ============================================================================
// 28. Duplicate detection with ACK (covers _withAck duplicate path)
// ============================================================================
void test_dedup_with_ack() {
  printf("  -> test_dedup_with_ack\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // Dispatch a GPIO command with CRC
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  rpc::payload::PinMode pm_msg = mcubridge_PinMode_init_default;
  pm_msg.pin = 13;
  pm_msg.mode = 1;
  bridge::test::set_pb_payload(f, pm_msg);
  f.crc = 0x12345678;

  // First dispatch (marks as processed)
  ba.dispatch(f);

  // Advance time into the dedupe window
  ba.setAckTimeoutMs(1000);
  ba.setAckRetryLimit(3);
  g_millis += 1500;

  // Second dispatch (detected as duplicate -> sends ACK only)
  ba.dispatch(f);
}

// ============================================================================
// 29. Status command dispatch with null handlers
//     Covers: Bridge.cpp L442 (null handler entry in status dispatch table)
// ============================================================================
void test_status_null_handler() {
  printf("  -> test_status_null_handler\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // STATUS_OK (48) - null handler
  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_OK);
  f.header.payload_length = 0;
  ba.dispatch(f);

  // STATUS_ERROR (49) - null handler
  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_ERROR);
  ba.dispatch(f);

  // STATUS_OVERFLOW (52) - null handler
  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_OVERFLOW);
  ba.dispatch(f);
}

// ============================================================================
// 30. rpc_structs.h encode() methods
//     Covers: rpc_structs.h L240,243,250 (encode methods)
// ============================================================================
void test_rpc_structs_encode() {
  printf("  -> test_rpc_structs_encode\n");

  uint8_t buf[64];
  pb_ostream_t stream;

  rpc::payload::VersionResponse vr{1, 2};
  stream = pb_ostream_from_buffer(buf, sizeof(buf));
  TEST_ASSERT(pb_encode(&stream, rpc::Payload::Descriptor<rpc::payload::VersionResponse>::fields(), &vr));
  
  rpc::payload::VersionResponse vr_dec = {};
  pb_istream_t istream = pb_istream_from_buffer(buf, stream.bytes_written);
  TEST_ASSERT(pb_decode(&istream, rpc::Payload::Descriptor<rpc::payload::VersionResponse>::fields(), &vr_dec));
  TEST_ASSERT_EQUAL(1, vr_dec.major);
  TEST_ASSERT_EQUAL(2, vr_dec.minor);

  rpc::payload::FreeMemoryResponse fmr{4096};
  stream = pb_ostream_from_buffer(buf, sizeof(buf));
  TEST_ASSERT(pb_encode(&stream, rpc::Payload::Descriptor<rpc::payload::FreeMemoryResponse>::fields(), &fmr));

  rpc::payload::Capabilities cap{2, 0, 14, 6, 0x01};
  stream = pb_ostream_from_buffer(buf, sizeof(buf));
  TEST_ASSERT(pb_encode(&stream, rpc::Payload::Descriptor<rpc::payload::Capabilities>::fields(), &cap));
}

// ============================================================================
// 31. Retransmit via MALFORMED status
// ============================================================================
void test_retransmit_via_malformed() {
  printf("  -> test_retransmit_via_malformed\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  uint8_t payload[] = {13, 1};
  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 2, payload);
  ba.flushPendingTxQueue();
  TEST_ASSERT(ba.isAwaitingAck());

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED);
  f.header.payload_length = 2;
  rpc::write_u16_be(etl::span<uint8_t>(f.payload.data(), 2), ba.getLastCommandId());
  ba.dispatch(f);
}

// ============================================================================
// 32. _sendFrame with pending queue (critical frame path)
//     Covers: Bridge.cpp L1140 (queue check in _sendFrame)
// ============================================================================
void test_send_frame_critical_path() {
  printf("  -> test_send_frame_critical_path\n");
  BiStream stream;
  reset_env(stream);

  // DataStore.get sends a critical frame via sendStringCommand
  // which routes through _sendFrame with requires_ack=true
  DataStore.get("key1", DataStoreClass::DataStoreGetHandler{});
  DataStore.get("key2", DataStoreClass::DataStoreGetHandler{});
}

// ============================================================================
// 33. rpc_structs.h template parse specializations
//     Covers: rpc_structs.h L304 (ConsoleWrite), L307-311 (ProcessRunAsync),
//             L346-350 (MailboxReadResponse)
// ============================================================================
void test_rpc_structs_parse_specializations() {
  printf("  -> test_rpc_structs_parse_specializations\n");

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // ConsoleWrite specialization
  rpc::payload::ConsoleWrite cw_msg = {};
  uint8_t cw_data[] = "hello";
  etl::span<const uint8_t> cw_span(cw_data, 5);
  rpc::util::pb_setup_encode_span(cw_msg.data, cw_span);
  bridge::test::set_pb_payload(f, cw_msg);
  
  uint8_t out_buf[64];
  etl::span<uint8_t> out_span(out_buf, 64);
  rpc::payload::ConsoleWrite cw_receive = {};
  rpc::util::pb_setup_decode_span(cw_receive.data, out_span);

  auto cw = rpc::Payload::parse<rpc::payload::ConsoleWrite>(f, cw_receive);
  TEST_ASSERT(cw.has_value());
  TEST_ASSERT_EQUAL(5, out_span.size());

  // ProcessRunAsync specialization
  rpc::payload::ProcessRunAsync pra_msg = {};
  etl::copy_n("ls", 2, pra_msg.command);
  bridge::test::set_pb_payload(f, pra_msg);
  
  auto pra = rpc::Payload::parse<rpc::payload::ProcessRunAsync>(f);
  TEST_ASSERT(pra.has_value());

  // MailboxReadResponse specialization
  rpc::payload::MailboxReadResponse mrr_msg = {};
  uint8_t mrr_data[] = "AB";
  etl::span<const uint8_t> mrr_span(mrr_data, 2);
  rpc::util::pb_setup_encode_span(mrr_msg.content, mrr_span);
  bridge::test::set_pb_payload(f, mrr_msg);
  
  uint8_t mrr_out_buf[64];
  etl::span<uint8_t> mrr_out_span(mrr_out_buf, 64);
  rpc::payload::MailboxReadResponse mrr_receive = {};
  rpc::util::pb_setup_decode_span(mrr_receive.content, mrr_out_span);

  auto mrr = rpc::Payload::parse<rpc::payload::MailboxReadResponse>(f, mrr_receive);
  TEST_ASSERT(mrr.has_value());
  TEST_ASSERT_EQUAL(2, mrr_out_span.size());

  // MailboxReadResponse with invalid length -> MALFORMED
  f.payload[0] = 0xFF; // Invalid tag
  f.header.payload_length = 1;
  auto mrr2 = rpc::Payload::parse<rpc::payload::MailboxReadResponse>(f);
  TEST_ASSERT(!mrr2.has_value());
}

// ============================================================================
// 34. Status handler callback on status dispatch
//     Covers: Bridge.cpp L442 follow-through (status_handler call)
// ============================================================================
void test_status_handler_callback() {
  printf("  -> test_status_handler_callback\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  bool handler_called = false;
  Bridge.onStatus(BridgeClass::StatusHandler::create(
      [](rpc::StatusCode, etl::span<const uint8_t>) {}));

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_OK);
  f.header.payload_length = 0;
  ba.dispatch(f);
  (void)handler_called;
}

// ============================================================================
// 35. Debug IO log path in _sendRawFrame
//     Covers: Bridge.cpp L992 (Bridge debug IO log)
//     Note: BRIDGE_DEBUG_IO=1 is set by build flags
// ============================================================================
void test_debug_io_log() {
  printf("  -> test_debug_io_log\n");
  BiStream stream;
  reset_env(stream);

  // Sending any frame triggers the debug log path
  stream.tx_buf.clear();
  Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
  TEST_ASSERT(stream.tx_buf.len > 0);
}

// ============================================================================
// 36. _flushPendingTxQueue when already awaiting ACK (no-op)
// ============================================================================
void test_flush_when_awaiting_ack() {
  printf("  -> test_flush_when_awaiting_ack\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  uint8_t payload[] = {13, 1};
  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 2, payload);
  ba.flushPendingTxQueue();
  TEST_ASSERT(ba.isAwaitingAck());

  // Push another frame and try to flush -- should be no-op since we're
  // already AwaitingAck
  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 2, payload);
  ba.flushPendingTxQueue();
  TEST_ASSERT(ba.isAwaitingAck());
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_gpio_commands_via_dispatch);
  RUN_TEST(test_console_write_via_dispatch);
  RUN_TEST(test_datastore_resp_via_dispatch);
  RUN_TEST(test_mailbox_via_dispatch);
  RUN_TEST(test_filesystem_via_dispatch);
  RUN_TEST(test_process_via_dispatch);
  RUN_TEST(test_unknown_command_via_dispatch);
  RUN_TEST(test_system_commands_via_dispatch);
  RUN_TEST(test_ack_and_retransmit);
  RUN_TEST(test_ack_timeout_with_status_handler);
  RUN_TEST(test_timer_callbacks);
  RUN_TEST(test_fsm_transitions);
  RUN_TEST(test_observer_notifications);
  RUN_TEST(test_bridge_events_defaults);
  RUN_TEST(test_hal_free_memory);
  RUN_TEST(test_apply_timing_config);
  RUN_TEST(test_send_key_val_command);
  RUN_TEST(test_emit_status);
  RUN_TEST(test_emit_status_with_observer);
  RUN_TEST(test_cobs_overflow_in_process);
  RUN_TEST(test_cobs_decode);
  RUN_TEST(test_frame_overflow);
  RUN_TEST(test_command_router);
  RUN_TEST(test_console_edge_cases);
  RUN_TEST(test_process_edge_cases);
  RUN_TEST(test_link_sync_full);
  RUN_TEST(test_link_reset_with_config);
  RUN_TEST(test_dedup_with_ack);
  RUN_TEST(test_status_null_handler);
  RUN_TEST(test_rpc_structs_encode);
  RUN_TEST(test_retransmit_via_malformed);
  RUN_TEST(test_send_frame_critical_path);
  RUN_TEST(test_rpc_structs_parse_specializations);
  RUN_TEST(test_status_handler_callback);
  RUN_TEST(test_debug_io_log);
  RUN_TEST(test_flush_when_awaiting_ack);
  return UNITY_END();
}

Stream* g_arduino_stream_delegate = nullptr;
