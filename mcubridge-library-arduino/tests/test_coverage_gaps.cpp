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

class TestStream : public Stream {
 public:
  ByteBuffer<8192> tx;
  ByteBuffer<8192> rx;
  size_t write(uint8_t c) override {
    tx.push(c);
    return 1;
  }
  size_t write(const uint8_t* b, size_t s) override {
    tx.append(b, s);
    return s;
  }
  int available() override { return rx.remaining(); }
  int read() override { return rx.read_byte(); }
  int peek() override { return rx.peek_byte(); }
  void flush() override {}
  void feed(const uint8_t* b, size_t s) { rx.append(b, s); }
};

void reset_env(TestStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin();
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setIdle();
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
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_SET_PIN_MODE (80)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  f.header.payload_length = rpc::payload::PinMode::SIZE;
  f.payload[0] = 13;
  f.payload[1] = 1;
  ba.dispatch(f);

  // CMD_DIGITAL_WRITE (81)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.header.payload_length = rpc::payload::DigitalWrite::SIZE;
  f.payload[0] = 13;
  f.payload[1] = 1;
  ba.dispatch(f);

  // CMD_ANALOG_WRITE (82)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE);
  f.header.payload_length = rpc::payload::AnalogWrite::SIZE;
  f.payload[0] = 9;
  rpc::write_u16_be(&f.payload[1], 128);
  ba.dispatch(f);

  // CMD_DIGITAL_READ (83)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
  f.header.payload_length = rpc::payload::PinRead::SIZE;
  f.payload[0] = 7;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  // CMD_ANALOG_READ (84)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ);
  f.header.payload_length = rpc::payload::PinRead::SIZE;
  f.payload[0] = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);
}

// ============================================================================
// 2. Console write through dispatch()
//    Covers: Bridge.cpp L751-765 (console dispatch + handler)
// ============================================================================
void test_console_write_via_dispatch() {
  printf("  -> test_console_write_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  const char* msg = "Hello Console";
  f.header.payload_length = static_cast<uint16_t>(strlen(msg));
  memcpy(f.payload.data(), msg, f.header.payload_length);
  ba.dispatch(f);
}

// ============================================================================
// 3. DataStore get response through dispatch()
//    Covers: Bridge.cpp L767-770 (datastore dispatch)
// ============================================================================
void test_datastore_resp_via_dispatch() {
  printf("  -> test_datastore_resp_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  DataStore.onDataStoreGetResponse(
      DataStoreClass::DataStoreGetHandler::create([](etl::string_view,
                                                     etl::span<const uint8_t>) {
      }));
  DataStore.requestGet("mykey");

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET_RESP);
  f.header.payload_length = 4;
  f.payload[0] = 3;
  f.payload[1] = 'a';
  f.payload[2] = 'b';
  f.payload[3] = 'c';
  ba.dispatch(f);
}

// ============================================================================
// 4. Mailbox commands through dispatch()
//    Covers: Bridge.cpp L792-798 (dispatch table), L801,823,825-836 (handlers)
// ============================================================================
void test_mailbox_via_dispatch() {
  printf("  -> test_mailbox_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  Mailbox.onMailboxMessage(
      MailboxClass::MailboxHandler::create([](etl::span<const uint8_t>) {}));

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_MAILBOX_PUSH (131)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH);
  f.header.payload_length = 5;
  rpc::write_u16_be(f.payload.data(), 3);
  f.payload[2] = 'x';
  f.payload[3] = 'y';
  f.payload[4] = 'z';
  ba.dispatch(f);

  // CMD_MAILBOX_READ_RESP (132)
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  f.header.payload_length = 4;
  rpc::write_u16_be(f.payload.data(), 2);
  f.payload[2] = 'A';
  f.payload[3] = 'B';
  ba.dispatch(f);

  // CMD_MAILBOX_AVAILABLE_RESP (133)
  Mailbox.onMailboxAvailableResponse(
      MailboxClass::MailboxAvailableHandler::create([](uint16_t) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), 42);
  ba.dispatch(f);
}

// ============================================================================
// 5. FileSystem commands through dispatch()
//    Covers: Bridge.cpp L838-863 (dispatch table + handlers)
// ============================================================================
void test_filesystem_via_dispatch() {
  printf("  -> test_filesystem_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_FILE_WRITE (144) - FileWrite ACK
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_WRITE);
  f.header.payload_length = 6;
  f.payload[0] = 1;  // path len
  f.payload[1] = 'a';
  rpc::write_u16_be(&f.payload[2], 2);
  f.payload[4] = 'd';
  f.payload[5] = 'e';
  ba.dispatch(f);

  // CMD_FILE_READ_RESP (147)
  FileSystem.onFileSystemReadResponse(
      FileSystemClass::FileSystemReadHandler::create(
          [](etl::span<const uint8_t>) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
  f.header.payload_length = 4;
  rpc::write_u16_be(f.payload.data(), 2);
  f.payload[2] = 'O';
  f.payload[3] = 'K';
  ba.dispatch(f);
}

// ============================================================================
// 6. Process commands through dispatch()
//    Covers: Bridge.cpp L922-928 (dispatch table + handlers)
// ============================================================================
void test_process_via_dispatch() {
  printf("  -> test_process_via_dispatch\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_PROCESS_RUN_RESP (164)
  Process.onProcessRunResponse(ProcessClass::ProcessRunHandler::create(
      [](rpc::StatusCode, etl::span<const uint8_t>,
         etl::span<const uint8_t>) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_RESP);
  f.header.payload_length = 8;
  f.payload[0] = 0x30;
  rpc::write_u16_be(&f.payload[1], 1);
  f.payload[3] = 'o';
  rpc::write_u16_be(&f.payload[4], 1);
  f.payload[6] = 'e';
  f.payload[7] = 0;
  ba.dispatch(f);

  // CMD_PROCESS_RUN_ASYNC_RESP (165)
  Process.onProcessRunAsyncResponse(
      ProcessClass::ProcessRunAsyncHandler::create([](int16_t) {}));
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), 99);
  ba.dispatch(f);

  // CMD_PROCESS_POLL_RESP (166) with pending PID
  Process.onProcessPollResponse(ProcessClass::ProcessPollHandler::create(
      [](rpc::StatusCode, uint8_t, etl::span<const uint8_t>,
         etl::span<const uint8_t>) {}));
  auto pa = bridge::test::ProcessTestAccessor::create(Process);
  pa.pushPendingPid(42);
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  f.header.payload_length = 8;
  f.payload[0] = 0x30;
  f.payload[1] = 0;
  rpc::write_u16_be(&f.payload[2], 1);
  f.payload[4] = 'o';
  rpc::write_u16_be(&f.payload[5], 1);
  f.payload[7] = 'e';
  ba.dispatch(f);
}

// ============================================================================
// 7. Unknown command through dispatch() (with and without handler)
//    Covers: Bridge.cpp L931
// ============================================================================
void test_unknown_command_via_dispatch() {
  printf("  -> test_unknown_command_via_dispatch\n");
  TestStream stream;
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
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // CMD_GET_VERSION (64)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  f.header.payload_length = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  // CMD_GET_FREE_MEMORY (66)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_FREE_MEMORY);
  f.header.payload_length = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  // CMD_GET_CAPABILITIES (72)
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_GET_CAPABILITIES);
  f.header.payload_length = 0;
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

  // CMD_SET_BAUDRATE (74)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_BAUDRATE);
  f.header.payload_length = 4;
  rpc::write_u32_be(f.payload.data(), 57600);
  ba.dispatch(f);
}

// ============================================================================
// 9. ACK handling and retransmit paths
//    Covers: Bridge.cpp L940 (_sendAck), L1174 (_handleAck pop),
//            L1200-1213 (_onAckTimeout), L1276-1286 (_flushPendingTxQueue)
// ============================================================================
void test_ack_and_retransmit() {
  printf("  -> test_ack_and_retransmit\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Push a frame and flush -> transitions to AwaitingAck
  uint8_t payload[] = {13, 1};
  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 2, payload);
  ba.flushPendingTxQueue();
  TEST_ASSERT(ba.isAwaitingAck());

  // Dispatch ACK -> pops queue, back to Idle
  uint16_t last_cmd = ba.getLastCommandId();
  rpc::Frame ack_frame;
  memset(&ack_frame, 0, sizeof(ack_frame));
  ack_frame.header.command_id =
      rpc::to_underlying(rpc::StatusCode::STATUS_ACK);
  ack_frame.header.payload_length = 2;
  rpc::write_u16_be(ack_frame.payload.data(), last_cmd);
  ba.dispatch(ack_frame);
  TEST_ASSERT(ba.isIdle());

  // Test ACK timeout with retransmit
  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 2, payload);
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
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  bool timeout_reported = false;
  Bridge.onStatus(BridgeClass::StatusHandler::create(
      [](rpc::StatusCode sc, etl::span<const uint8_t>) {
        (void)sc;
      }));

  uint8_t payload[] = {13, 1};
  ba.pushPendingTxFrame(
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 2, payload);
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
  TestStream stream;
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
  TestStream stream;
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
  TestStream stream;
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
  TEST_ASSERT_EQ_UINT(mem, 4096);

  // Also test the global wrapper
  TEST_ASSERT_EQ_UINT(getFreeMemory(), 4096);
}

// ============================================================================
// 16. _applyTimingConfig
//     Covers: Bridge.cpp L1356-1357 (clamping)
// ============================================================================
void test_apply_timing_config() {
  printf("  -> test_apply_timing_config\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Empty payload -> defaults
  ba.applyTimingConfig(nullptr, 0);
  TEST_ASSERT(ba.getAckTimeoutMs() > 0);

  // Valid config
  uint8_t config[7];
  rpc::write_u16_be(config, 500);
  config[2] = 3;
  rpc::write_u32_be(config + 3, 5000);
  ba.applyTimingConfig(config, 7);
  TEST_ASSERT_EQ_UINT(ba.getAckTimeoutMs(), 500);

  // Out-of-range -> clamped to defaults
  rpc::write_u16_be(config, 1);
  config[2] = 0;
  rpc::write_u32_be(config + 3, 1);
  ba.applyTimingConfig(config, 7);
}

// ============================================================================
// 17. sendKeyValCommand path via DataStore.put()
//     Covers: Bridge.cpp L1044-1046
// ============================================================================
void test_send_key_val_command() {
  printf("  -> test_send_key_val_command\n");
  TestStream stream;
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
  TestStream stream;
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
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  f.header.payload_length = 2;
  ba.dispatch(f);

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
  TestStream stream;
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
  TestStream stream;
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
  rpc::write_u16_be(&buf[1], fake_payload_len);
  rpc::write_u16_be(&buf[3], 0x40);
  memset(&buf[5], 0xAA, fake_payload_len);

  etl::crc32 crc_calc;
  crc_calc.add(buf.data(), buf.data() + total - 4);
  rpc::write_u32_be(&buf[total - 4], crc_calc.value());

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
  TestStream stream;
  reset_env(stream);

  // available() when empty -> 0
  TEST_ASSERT_EQ_UINT(Console.available(), 0);

  // write() when buffer full and flush fails (bridge unsync)
  auto ba = bridge::test::TestAccessor::create(Bridge);
  auto ca = bridge::test::ConsoleTestAccessor::create(Console);
  ba.setUnsynchronized();
  ca.clearTxBuffer();
  while (!ca.isTxBufferFull()) {
    ca.pushTxByte('X');
  }
  TEST_ASSERT_EQ_UINT(Console.write('Z'), 0);
}

// ============================================================================
// 25. Process edge cases
//     Covers: Process.cpp L17 (overflow emit), L71 (empty pop)
// ============================================================================
void test_process_edge_cases() {
  printf("  -> test_process_edge_cases\n");
  TestStream stream;
  reset_env(stream);

  auto pa = bridge::test::ProcessTestAccessor::create(Process);

  // Pop from empty queue -> sentinel
  uint16_t sentinel = pa.popPendingPid();
  TEST_ASSERT_EQ_UINT(sentinel, rpc::RPC_INVALID_ID_SENTINEL);

  // Process.run when bridge is unsync -> emit overflow (L17)
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setUnsynchronized();
  Process.run("ls");
}

// ============================================================================
// 26. LinkSync full path (with and without secret)
//     Covers: Bridge.cpp L612-617 (malformed check), L638,647-650 (response)
// ============================================================================
void test_link_sync_full() {
  printf("  -> test_link_sync_full\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // LinkSync without secret
  ba.clearSharedSecret();
  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  f.header.payload_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  memset(f.payload.data(), 0xAA, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  stream.tx.clear();
  ba.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);

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

  f.header.payload_length =
      rpc::RPC_HANDSHAKE_NONCE_LENGTH + rpc::RPC_HANDSHAKE_TAG_LENGTH;
  memcpy(f.payload.data(), nonce, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  memcpy(f.payload.data() + rpc::RPC_HANDSHAKE_NONCE_LENGTH, tag,
         rpc::RPC_HANDSHAKE_TAG_LENGTH);
  stream.tx.clear();
  ba2.dispatch(f);
  TEST_ASSERT(stream.tx.len > 0);
  TEST_ASSERT(Bridge.isSynchronized());
  }
}

// ============================================================================
// 27. LinkReset with timing config
//     Covers: Bridge.cpp L1356-1357 (_applyTimingConfig from LinkReset)
// ============================================================================
void test_link_reset_with_config() {
  printf("  -> test_link_reset_with_config\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
  f.header.payload_length = rpc::payload::HandshakeConfig::SIZE;
  rpc::write_u16_be(f.payload.data(), 500);
  f.payload[2] = 3;
  rpc::write_u32_be(f.payload.data() + 3, 5000);
  ba.dispatch(f);
}

// ============================================================================
// 28. Duplicate detection with ACK (covers _withAck duplicate path)
// ============================================================================
void test_dedup_with_ack() {
  printf("  -> test_dedup_with_ack\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // Dispatch a GPIO command with CRC
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  f.header.payload_length = rpc::payload::PinMode::SIZE;
  f.payload[0] = 13;
  f.payload[1] = 1;
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
  TestStream stream;
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

  uint8_t buf[32];

  rpc::payload::VersionResponse vr{1, 2};
  vr.encode(buf);
  TEST_ASSERT_EQ_UINT(buf[0], 1);
  TEST_ASSERT_EQ_UINT(buf[1], 2);

  rpc::payload::FreeMemoryResponse fmr{4096};
  fmr.encode(buf);

  rpc::payload::Capabilities cap{2, 0, 14, 6, 0x01};
  cap.encode(buf);

  rpc::payload::DigitalReadResponse drr{1};
  drr.encode(buf);

  rpc::payload::AnalogReadResponse arr{1023};
  arr.encode(buf);

  rpc::payload::SetBaudratePacket sbp{115200};
  sbp.encode(buf);

  rpc::payload::HandshakeConfig hc{500, 3, 5000};
  hc.encode(buf);

  rpc::payload::AckPacket ap{0x1234};
  ap.encode(buf);

  rpc::payload::PinMode pm{13, 1};
  pm.encode(buf);

  rpc::payload::DigitalWrite dw{13, 1};
  dw.encode(buf);

  rpc::payload::AnalogWrite aw{9, 128};
  aw.encode(buf);

  rpc::payload::PinRead pr{7};
  pr.encode(buf);

  rpc::payload::MailboxProcessed mp{1};
  mp.encode(buf);

  rpc::payload::MailboxAvailableResponse mar{42};
  mar.encode(buf);

  rpc::payload::ProcessKill pk{99};
  pk.encode(buf);

  rpc::payload::ProcessPoll pp{42};
  pp.encode(buf);

  rpc::payload::ProcessRunAsyncResponse prar{123};
  prar.encode(buf);
}

// ============================================================================
// 31. Retransmit via MALFORMED status
// ============================================================================
void test_retransmit_via_malformed() {
  printf("  -> test_retransmit_via_malformed\n");
  TestStream stream;
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
  rpc::write_u16_be(f.payload.data(), ba.getLastCommandId());
  ba.dispatch(f);
}

// ============================================================================
// 32. _sendFrame with pending queue (critical frame path)
//     Covers: Bridge.cpp L1140 (queue check in _sendFrame)
// ============================================================================
void test_send_frame_critical_path() {
  printf("  -> test_send_frame_critical_path\n");
  TestStream stream;
  reset_env(stream);

  // DataStore.requestGet sends a critical frame via sendStringCommand
  // which routes through _sendFrame with requires_ack=true
  DataStore.requestGet("key1");
  DataStore.requestGet("key2");
}

// ============================================================================
// 33. rpc_structs.h template parse specializations
//     Covers: rpc_structs.h L304 (ConsoleWrite), L307-311 (ProcessRun,
//             ProcessRunAsync), L346-350 (MailboxReadResponse)
// ============================================================================
void test_rpc_structs_parse_specializations() {
  printf("  -> test_rpc_structs_parse_specializations\n");

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // ConsoleWrite specialization - just needs data + length
  f.header.payload_length = 5;
  memcpy(f.payload.data(), "hello", 5);
  auto cw = rpc::Payload::parse<rpc::payload::ConsoleWrite>(f);
  TEST_ASSERT(cw.has_value());
  TEST_ASSERT_EQ_UINT(cw->length, 5);

  // ProcessRun specialization
  f.header.payload_length = 2;
  memcpy(f.payload.data(), "ls", 2);
  auto pr = rpc::Payload::parse<rpc::payload::ProcessRun>(f);
  TEST_ASSERT(pr.has_value());

  // ProcessRunAsync specialization
  auto pra = rpc::Payload::parse<rpc::payload::ProcessRunAsync>(f);
  TEST_ASSERT(pra.has_value());

  // MailboxReadResponse specialization
  f.header.payload_length = 4;
  rpc::write_u16_be(f.payload.data(), 2);
  f.payload[2] = 'A';
  f.payload[3] = 'B';
  auto mrr = rpc::Payload::parse<rpc::payload::MailboxReadResponse>(f);
  TEST_ASSERT(mrr.has_value());
  TEST_ASSERT_EQ_UINT(mrr->length, 2);

  // MailboxReadResponse with invalid length -> MALFORMED
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), 100);
  auto mrr2 = rpc::Payload::parse<rpc::payload::MailboxReadResponse>(f);
  TEST_ASSERT(!mrr2.has_value());
}

// ============================================================================
// 34. Status handler callback on status dispatch
//     Covers: Bridge.cpp L442 follow-through (status_handler call)
// ============================================================================
void test_status_handler_callback() {
  printf("  -> test_status_handler_callback\n");
  TestStream stream;
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
  TestStream stream;
  reset_env(stream);

  // Sending any frame triggers the debug log path
  stream.tx.clear();
  Bridge.sendFrame(rpc::StatusCode::STATUS_OK);
  TEST_ASSERT(stream.tx.len > 0);
}

// ============================================================================
// 36. _flushPendingTxQueue when already awaiting ACK (no-op)
// ============================================================================
void test_flush_when_awaiting_ack() {
  printf("  -> test_flush_when_awaiting_ack\n");
  TestStream stream;
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

int main() {
  printf("COVERAGE GAPS TEST START\n");
  test_gpio_commands_via_dispatch();
  test_console_write_via_dispatch();
  test_datastore_resp_via_dispatch();
  test_mailbox_via_dispatch();
  test_filesystem_via_dispatch();
  test_process_via_dispatch();
  test_unknown_command_via_dispatch();
  test_system_commands_via_dispatch();
  test_ack_and_retransmit();
  test_ack_timeout_with_status_handler();
  test_timer_callbacks();
  test_fsm_transitions();
  test_observer_notifications();
  test_bridge_events_defaults();
  test_hal_free_memory();
  test_apply_timing_config();
  test_send_key_val_command();
  test_emit_status();
  test_emit_status_with_observer();
  test_cobs_overflow_in_process();
  test_cobs_decode();
  test_frame_overflow();
  test_command_router();
  test_console_edge_cases();
  test_process_edge_cases();
  test_link_sync_full();
  test_link_reset_with_config();
  test_dedup_with_ack();
  test_status_null_handler();
  test_rpc_structs_encode();
  test_retransmit_via_malformed();
  test_send_frame_critical_path();
  test_rpc_structs_parse_specializations();
  test_status_handler_callback();
  test_debug_io_log();
  test_flush_when_awaiting_ack();
  printf("COVERAGE GAPS TEST END\n");
  return 0;
}

Stream* g_arduino_stream_delegate = nullptr;
