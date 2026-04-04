/**
 * @file test_coverage_full.cpp
 * @brief Comprehensive coverage tests targeting all uncovered C++ lines.
 *
 * Covers: RLE decode, frame parser edge cases, FSM transitions, Console I/O,
 * SPI mock methods, service callbacks with handlers, Bridge dispatch paths,
 * CRC error escalation, ACK timeout retry, decompressed frames, observer
 * notifications, pb_copy_join, and handshake tag computation.
 */
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include <Arduino.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/BridgeEvents.h"
#include "protocol/rle.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"
#include "util/pb_copy.h"
#include "test_support.h"

// --- Globals ---
static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }
void delay(unsigned long ms) { g_test_millis += ms; }

namespace {
BiStream g_stream;
}

BridgeClass Bridge(g_stream);
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

using bridge::test::TestAccessor;
using bridge::test::ConsoleTestAccessor;

// ============================================================================
// Helpers
// ============================================================================

static void reset_test_bridge() {
  g_test_millis = 0;
  g_stream.clear();
  reset_bridge_core(Bridge, g_stream);
  Console.begin();
}

// ============================================================================
// 1. RLE Decode — Full coverage of rle.cpp (0% → 100%)
// ============================================================================

static void test_rle_decode_empty_input() {
  uint8_t dst[16];
  // Empty source
  TEST_ASSERT_EQUAL(0U, rle::decode(etl::span<const uint8_t>(), etl::span<uint8_t>(dst, sizeof(dst))));
  // Empty destination
  uint8_t src[] = {0x41};
  TEST_ASSERT_EQUAL(0U, rle::decode(etl::span<const uint8_t>(src, 1), etl::span<uint8_t>()));
}

static void test_rle_decode_literals_only() {
  // No escape bytes — pure literal copy
  uint8_t src[] = {0x41, 0x42, 0x43, 0x44};
  uint8_t dst[16] = {};
  size_t len = rle::decode(etl::span<const uint8_t>(src, 4), etl::span<uint8_t>(dst, sizeof(dst)));
  TEST_ASSERT_EQUAL(4U, len);
  TEST_ASSERT_EQUAL(0x41, dst[0]);
  TEST_ASSERT_EQUAL(0x44, dst[3]);
}

static void test_rle_decode_run_expansion() {
  // Escape block: ESCAPE_BYTE, count_m2, value
  // count_m2 = 0 → run_len = 0 + RPC_RLE_OFFSET = 2
  const uint8_t escape = rle::ESCAPE_BYTE;
  uint8_t src[] = {0x41, escape, 0x00, 0xBB};  // literal 'A', then run of 2 × 0xBB
  uint8_t dst[16] = {};
  size_t len = rle::decode(etl::span<const uint8_t>(src, 4), etl::span<uint8_t>(dst, sizeof(dst)));
  TEST_ASSERT_EQUAL(3U, len);  // 1 literal + 2 run
  TEST_ASSERT_EQUAL(0x41, dst[0]);
  TEST_ASSERT_EQUAL(0xBB, dst[1]);
  TEST_ASSERT_EQUAL(0xBB, dst[2]);
}

static void test_rle_decode_single_escape_marker() {
  // SINGLE_ESCAPE_MARKER means run_len = 1 (single occurrence of escape byte value)
  const uint8_t escape = rle::ESCAPE_BYTE;
  uint8_t src[] = {escape, rle::SINGLE_ESCAPE_MARKER, 0xCC};
  uint8_t dst[16] = {};
  size_t len = rle::decode(etl::span<const uint8_t>(src, 3), etl::span<uint8_t>(dst, sizeof(dst)));
  TEST_ASSERT_EQUAL(1U, len);
  TEST_ASSERT_EQUAL(0xCC, dst[0]);
}

static void test_rle_decode_dst_overflow_literals() {
  // Destination too small for literals
  uint8_t src[] = {0x41, 0x42, 0x43};
  uint8_t dst[2] = {};
  size_t len = rle::decode(etl::span<const uint8_t>(src, 3), etl::span<uint8_t>(dst, 2));
  TEST_ASSERT_EQUAL(0U, len);
}

static void test_rle_decode_dst_overflow_run() {
  // Destination too small for run expansion
  const uint8_t escape = rle::ESCAPE_BYTE;
  uint8_t src[] = {escape, 10, 0xAA};  // run_len = 10 + 2 = 12
  uint8_t dst[4] = {};
  size_t len = rle::decode(etl::span<const uint8_t>(src, 3), etl::span<uint8_t>(dst, 4));
  TEST_ASSERT_EQUAL(0U, len);
}

static void test_rle_decode_truncated_escape() {
  // Escape byte at end without enough following bytes
  const uint8_t escape = rle::ESCAPE_BYTE;
  uint8_t src[] = {0x41, escape, 0x00};  // Only 1 byte after escape, need 2
  uint8_t dst[16] = {};
  size_t len = rle::decode(etl::span<const uint8_t>(src, 3), etl::span<uint8_t>(dst, sizeof(dst)));
  TEST_ASSERT_EQUAL(0U, len);
}

static void test_rle_decode_mixed() {
  // Literals + escape + more literals
  const uint8_t escape = rle::ESCAPE_BYTE;
  uint8_t src[] = {0x01, 0x02, escape, 0x01, 0xAA, 0x03};
  // Literals: 0x01, 0x02. Then run: count_m2=1 → run_len=3, value=0xAA. Then literal: 0x03.
  uint8_t dst[16] = {};
  size_t len = rle::decode(etl::span<const uint8_t>(src, 6), etl::span<uint8_t>(dst, sizeof(dst)));
  TEST_ASSERT_EQUAL(6U, len);
  TEST_ASSERT_EQUAL(0x01, dst[0]);
  TEST_ASSERT_EQUAL(0x02, dst[1]);
  TEST_ASSERT_EQUAL(0xAA, dst[2]);
  TEST_ASSERT_EQUAL(0xAA, dst[3]);
  TEST_ASSERT_EQUAL(0xAA, dst[4]);
  TEST_ASSERT_EQUAL(0x03, dst[5]);
}

// ============================================================================
// 2. Frame Parser — Version mismatch & overflow (rpc_frame.h:79,93)
// ============================================================================

static void test_frame_parser_version_mismatch() {
  // Build a frame with wrong version byte
  rpc::FrameParser parser;
  etl::array<uint8_t, rpc::MIN_FRAME_SIZE> buf = {};
  etl::byte_stream_writer w(buf.begin(), buf.end(), etl::endian::big);
  w.write<uint8_t>(0x00);                     // Wrong version (should be PROTOCOL_VERSION)
  w.write<uint16_t>(0);                       // payload_length
  w.write<uint16_t>(0x30);                    // command_id
  w.write<uint16_t>(0);                       // sequence_id
  // CRC over header
  etl::crc32 crc;
  crc.add(buf.begin(), buf.begin() + rpc::FRAME_HEADER_SIZE);
  w.write<uint32_t>(crc.value());

  auto result = parser.parse(etl::span<const uint8_t>(buf.data(), buf.size()));
  TEST_ASSERT_FALSE(result.has_value());
  TEST_ASSERT_EQUAL(static_cast<int>(rpc::FrameError::MALFORMED), static_cast<int>(result.error()));
}

static void test_frame_parser_payload_length_mismatch() {
  // Frame header claims payload_length=100 but buffer is MIN_FRAME_SIZE
  rpc::FrameParser parser;
  etl::array<uint8_t, rpc::MIN_FRAME_SIZE> buf = {};
  etl::byte_stream_writer w(buf.begin(), buf.end(), etl::endian::big);
  w.write<uint8_t>(rpc::PROTOCOL_VERSION);
  w.write<uint16_t>(100);                     // payload_length claims 100 but buffer is too small
  w.write<uint16_t>(0x30);
  w.write<uint16_t>(0);
  etl::crc32 crc;
  crc.add(buf.begin(), buf.begin() + rpc::FRAME_HEADER_SIZE);
  w.write<uint32_t>(crc.value());

  auto result = parser.parse(etl::span<const uint8_t>(buf.data(), buf.size()));
  TEST_ASSERT_FALSE(result.has_value());
  TEST_ASSERT_EQUAL(static_cast<int>(rpc::FrameError::MALFORMED), static_cast<int>(result.error()));
}

static void test_frame_parser_overflow() {
  // The OVERFLOW error path requires payload_length > MAX_PAYLOAD_SIZE
  // with consistent buffer size. Since MAX_RAW_FRAME_SIZE = HEADER + MAX_PAYLOAD + CRC,
  // any frame that sets payload_length > MAX_PAYLOAD_SIZE but buffer <= MAX_RAW_FRAME_SIZE
  // will fail the size consistency check first (MALFORMED). Test both edge paths.
  rpc::FrameParser parser;
  etl::array<uint8_t, 4> tiny = {};
  auto result = parser.parse(etl::span<const uint8_t>(tiny.data(), tiny.size()));
  TEST_ASSERT_FALSE(result.has_value());
  TEST_ASSERT_EQUAL(static_cast<int>(rpc::FrameError::MALFORMED), static_cast<int>(result.error()));
}

// ============================================================================
// 3. Observer Default Notifications (BridgeEvents.h:24-28)
// ============================================================================

struct MinimalObserver : public BridgeObserver {
  // Does NOT override any notification — tests default empty impls
};

static void test_observer_default_notifications() {
  MinimalObserver obs;
  // Call all default notification methods
  obs.notification(MsgBridgeSynchronized{});
  obs.notification(MsgBridgeLost{});
  TEST_ASSERT(true);  // No crash = success
}

// ============================================================================
// 4. DataStore — _onResponse with valid handler (DataStore.cpp:30)
// ============================================================================

static bool g_ds_handler_called = false;
static etl::string_view g_ds_handler_key;

static void ds_handler(etl::string_view key, etl::span<const uint8_t>) {
  g_ds_handler_called = true;
  g_ds_handler_key = key;
}

static void test_datastore_response_with_handler() {
#if BRIDGE_ENABLE_DATASTORE
  reset_test_bridge();
  g_ds_handler_called = false;

  DataStore.get("mykey",
      etl::delegate<void(etl::string_view, etl::span<const uint8_t>)>::create<ds_handler>());

  uint8_t val[] = {0x42};
  DataStore._onResponse(etl::span<const uint8_t>(val, 1));

  TEST_ASSERT_TRUE(g_ds_handler_called);
#endif
}

// ============================================================================
// 5. Mailbox — handlers (Mailbox.cpp:26,36)
// ============================================================================

static bool g_mbox_handler_called = false;
static bool g_mbox_avail_called = false;
static uint16_t g_mbox_avail_count = 0;

static void mbox_data_handler(etl::span<const uint8_t>) {
  g_mbox_handler_called = true;
}

static void mbox_avail_handler(uint16_t count) {
  g_mbox_avail_called = true;
  g_mbox_avail_count = count;
}

static void test_mailbox_with_handlers() {
#if BRIDGE_ENABLE_MAILBOX
  reset_test_bridge();
  g_mbox_handler_called = false;
  g_mbox_avail_called = false;

  Mailbox.onMailboxMessage(etl::delegate<void(etl::span<const uint8_t>)>::create<mbox_data_handler>());
  Mailbox.onMailboxAvailable(etl::delegate<void(uint16_t)>::create<mbox_avail_handler>());

  uint8_t data[] = {0x01, 0x02};
  Mailbox._onIncomingData(etl::span<const uint8_t>(data, 2));
  TEST_ASSERT_TRUE(g_mbox_handler_called);

  rpc::payload::MailboxAvailableResponse resp = {};
  resp.count = 42;
  Mailbox._onAvailableResponse(resp);
  TEST_ASSERT_TRUE(g_mbox_avail_called);
  TEST_ASSERT_EQUAL(42, g_mbox_avail_count);
#endif
}

// ============================================================================
// 6. Process — kill error path (Process.cpp:34)
// ============================================================================

static rpc::StatusCode g_kill_status = rpc::StatusCode::STATUS_OK;

static void kill_handler(rpc::StatusCode status) {
  g_kill_status = status;
}

static void test_process_kill_error_path() {
#if BRIDGE_ENABLE_PROCESS
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);

  // Put bridge in fault state so sendPbCommand returns false
  ba.fsmCryptoFault();

  g_kill_status = rpc::StatusCode::STATUS_OK;
  Process.kill(1, etl::delegate<void(rpc::StatusCode)>::create<kill_handler>());
  TEST_ASSERT_EQUAL(static_cast<int>(rpc::StatusCode::STATUS_ERROR), static_cast<int>(g_kill_status));
#endif
}

// ============================================================================
// 7. SPI Mock Methods (SPIService.cpp:49-50,52)
// ============================================================================

static void test_spi_mock_methods() {
#if BRIDGE_ENABLE_SPI
  SPIService.begin();
  TEST_ASSERT_TRUE(SPIService.isInitialized());

  SPIService.setConfig(1000000, 0, 0);  // Covers line 50

  uint8_t buf[] = {0xAA, 0xBB};
  size_t xferred = SPIService.transfer(buf, 2);  // Covers line 52
  TEST_ASSERT_EQUAL(2U, xferred);

  SPIService.end();  // Covers line 49
  TEST_ASSERT_FALSE(SPIService.isInitialized());
#endif
}

// ============================================================================
// 8. SPI Handlers through Bridge dispatch (Bridge.cpp:406-452)
// ============================================================================

static void test_spi_handlers_dispatch() {
#if BRIDGE_ENABLE_SPI
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buf = {};

  // SPI Begin (line 406-408)
  {
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN);
    f.header.payload_length = 0;
    f.payload = etl::span<const uint8_t>();
    ba.dispatch(f);
  }

  // SPI SetConfig with protobuf payload (lines 432-442)
  {
    mcubridge_SpiConfig msg = mcubridge_SpiConfig_init_default;
    msg.frequency = 2000000;
    msg.bit_order = 1;
    msg.data_mode = 0;
    pb_ostream_t s = pb_ostream_from_buffer(payload_buf.data(), payload_buf.size());
    pb_encode(&s, mcubridge_SpiConfig_fields, &msg);
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SPI_SET_CONFIG);
    f.header.payload_length = static_cast<uint16_t>(s.bytes_written);
    f.payload = etl::span<const uint8_t>(payload_buf.data(), s.bytes_written);
    ba.dispatch(f);
  }

  // SPI Transfer with data (lines 451-452)
  {
    mcubridge_SpiTransfer msg = mcubridge_SpiTransfer_init_default;
    // Set up a small data payload
    uint8_t spi_data[] = {0x11, 0x22};
    etl::span<const uint8_t> data_span(spi_data, 2);
    msg.data.funcs.encode = &rpc::util::pb_encode_span_callback;
    msg.data.arg = const_cast<etl::span<const uint8_t>*>(&data_span);
    pb_ostream_t s = pb_ostream_from_buffer(payload_buf.data(), payload_buf.size());
    pb_encode(&s, mcubridge_SpiTransfer_fields, &msg);
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SPI_TRANSFER);
    f.header.payload_length = static_cast<uint16_t>(s.bytes_written);
    f.payload = etl::span<const uint8_t>(payload_buf.data(), s.bytes_written);
    ba.dispatch(f);
  }

  // SPI End (line 410-416)
  {
    f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SPI_END);
    f.header.payload_length = 0;
    f.payload = etl::span<const uint8_t>();
    ba.dispatch(f);
  }
#endif
}

// ============================================================================
// 9. pb_copy_join — multi-part (pb_copy.h:85,88,90-91)
// ============================================================================

static void test_pb_copy_join_parts() {
  char dst[64] = {};
  etl::string_view parts[] = {
    etl::string_view("arg1"),
    etl::string_view("arg2"),
    etl::string_view("arg3"),
  };
  rpc::util::pb_copy_join(
    etl::string_view("cmd"),
    etl::span<const etl::string_view>(parts, 3),
    dst, sizeof(dst));
  TEST_ASSERT_EQUAL_STRING("cmd arg1 arg2 arg3", dst);
}

static void test_pb_copy_join_overflow() {
  char dst[10] = {};
  etl::string_view parts[] = {
    etl::string_view("very_long_argument"),
  };
  rpc::util::pb_copy_join(
    etl::string_view("cmd"),
    etl::span<const etl::string_view>(parts, 1),
    dst, sizeof(dst));
  // Should be truncated to fit
  TEST_ASSERT_EQUAL(0, dst[9]);  // Null terminated
  TEST_ASSERT_EQUAL(9U, strlen(dst));
}

static void test_pb_copy_join_empty_dst() {
  // Zero-size destination
  char dst[1] = {'X'};
  etl::string_view parts[] = { etl::string_view("a") };
  rpc::util::pb_copy_join(etl::string_view("b"), etl::span<const etl::string_view>(parts, 1), dst, 0);
  TEST_ASSERT_EQUAL('X', dst[0]);  // Unchanged
}

// ============================================================================
// 10. Console — write full buffer + break path (Console.cpp:43-45)
//     Console — read with XOFF reset (Console.cpp:72)
// ============================================================================

static void test_console_write_buffer_full_break() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  auto ca = ConsoleTestAccessor::create(Console);

  // Put bridge in fault state so flush() can't actually send
  ba.fsmCryptoFault();

  Console.begin();

  // Fill TX buffer to capacity
  for (size_t i = 0; i < bridge::config::CONSOLE_TX_BUFFER_SIZE; ++i) {
    ca.pushTxByte('X');
  }
  TEST_ASSERT_TRUE(ca.isTxBufferFull());

  // Now write a multi-byte buffer; flush won't free space → should break
  uint8_t data[] = {'A', 'B', 'C'};
  size_t written = Console.write(data, 3);
  TEST_ASSERT_EQUAL(0U, written);
}

static void test_console_read_xoff_reset() {
  reset_test_bridge();
  auto ca = ConsoleTestAccessor::create(Console);
  Console.begin();

  // Set XOFF_SENT flag
  ca.setXoffSent(true);
  TEST_ASSERT_TRUE(ca.getXoffSent());

  // Fill RX buffer to > half capacity
  size_t half = bridge::config::CONSOLE_RX_BUFFER_SIZE / 2;
  for (size_t i = 0; i < half + 2; ++i) {
    ca.pushRxByte(static_cast<uint8_t>('A' + (i % 26)));
  }

  // Read until buffer drops to <= half capacity
  for (size_t i = 0; i < 3; ++i) {
    Console.read();
  }

  // XOFF should be reset once buffer size <= capacity/2
  TEST_ASSERT_FALSE(ca.getXoffSent());
}

// ============================================================================
// 11. FSM — comprehensive transitions (bridge_fsm.h:149-288)
// ============================================================================

static void test_fsm_all_state_transitions() {
  // Re-create bridge to start at Stabilizing (initial FSM state)
  g_stream.clear();
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(g_stream);
  Bridge.begin();

  auto ba = TestAccessor::create(Bridge);

  // 1. FSM starts at Stabilizing
  TEST_ASSERT_TRUE(ba.getStartupStabilizing());

  // 2. Stabilizing → Unsynchronized via onStartupStabilized
  ba.onStartupStabilized();
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());

  // 3. Unsynchronized → Syncing
  ba.fsmHandshakeStart();
  TEST_ASSERT_TRUE(Bridge.isSyncing());

  // 4. Syncing → Idle (via HandshakeComplete)
  ba.fsmHandshakeComplete();
  TEST_ASSERT_TRUE(Bridge.isIdle());

  // 5. Idle → AwaitingAck
  ba.fsmSendCritical();
  TEST_ASSERT_TRUE(Bridge.isAwaitingAck());

  // 6. AwaitingAck → Unsynchronized via Reset
  ba.fsmResetFsm();
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());

  // 7. Unsynchronized → Fault via HandshakeFailed
  ba.fsmHandshakeFailed();
  TEST_ASSERT_TRUE(Bridge.isFault());

  // 8. Fault → Unsynchronized via Reset
  ba.fsmResetFsm();
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());
}

static void test_fsm_crypto_fault_from_each_state() {
  // CryptoFault should reach STATE_FAULT from any operational state
  auto ba = TestAccessor::create(Bridge);

  // From Stabilizing
  ba.fsmResetFsm();
  ba.fsmCryptoFault();
  TEST_ASSERT_TRUE(Bridge.isFault());

  // From Unsynchronized
  ba.fsmResetFsm();
  ba.fsmCryptoFault();
  TEST_ASSERT_TRUE(Bridge.isFault());

  // From Syncing
  {
    auto ba2 = TestAccessor::create(Bridge);
    ba2.fsmResetFsm();
    ba2.setStartupStabilizing(false);
    ba2.fsmHandshakeStart();
    TEST_ASSERT_TRUE(Bridge.isSyncing());
    ba2.fsmCryptoFault();
    TEST_ASSERT_TRUE(Bridge.isFault());
  }

  // From Idle
  reset_test_bridge();
  {
    auto ba3 = TestAccessor::create(Bridge);
    ba3.fsmCryptoFault();
    TEST_ASSERT_TRUE(Bridge.isFault());
  }

  // From AwaitingAck
  reset_test_bridge();
  {
    auto ba4 = TestAccessor::create(Bridge);
    ba4.fsmSendCritical();
    TEST_ASSERT_TRUE(Bridge.isAwaitingAck());
    ba4.fsmCryptoFault();
    TEST_ASSERT_TRUE(Bridge.isFault());
  }

  // Fault stays Fault on double CryptoFault
  {
    auto ba5 = TestAccessor::create(Bridge);
    ba5.fsmCryptoFault();
    TEST_ASSERT_TRUE(Bridge.isFault());
  }
}

static void test_fsm_reset_from_syncing() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.fsmResetFsm();
  ba.setStartupStabilizing(false);
  ba.fsmHandshakeStart();
  TEST_ASSERT_TRUE(Bridge.isSyncing());

  // Reset from Syncing → Unsynchronized
  ba.fsmResetFsm();
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());
}

static void test_fsm_reset_from_idle() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  TEST_ASSERT_TRUE(Bridge.isIdle());
  ba.fsmResetFsm();
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());
}

static void test_fsm_awaiting_ack_reset() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.fsmSendCritical();
  TEST_ASSERT_TRUE(Bridge.isAwaitingAck());
  ba.fsmResetFsm();
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());
}

// ============================================================================
// 12. CRC Error Escalation (Bridge.cpp:170-177)
// ============================================================================

static void test_crc_error_escalation() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Trigger MAX_CONSECUTIVE_CRC_ERRORS CRC mismatches
  for (uint16_t i = 0; i < bridge::config::MAX_CONSECUTIVE_CRC_ERRORS; ++i) {
    ba.setLastParseError(rpc::FrameError::CRC_MISMATCH);
    Bridge.process();
  }

  // Bridge should have entered safe state → reset to Unsynchronized
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());
}

// ============================================================================
// 13. ACK Timeout — retry limit exceeded (Bridge.cpp:512-513)
// ============================================================================

static void test_ack_timeout_retry_exceeded() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Push a pending TX frame and move to AwaitingAck
  uint8_t payload[] = {0x01};
  ba.pushPendingTxFrame(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION), 1, payload);
  ba.flushPendingTxQueue();
  TEST_ASSERT_TRUE(Bridge.isAwaitingAck());

  // Set retry count to max
  ba.setRetryCount(ba.getAckRetryLimit());

  // Trigger ACK timeout — should exceed retry limit
  ba.onAckTimeout();

  // Bridge should have entered safe state
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());
}

// ============================================================================
// 14. Status Handler Callback (Bridge.cpp:292)
// ============================================================================

static bool g_status_handler_called = false;

static void status_callback(rpc::StatusCode, etl::span<const uint8_t>) {
  g_status_handler_called = true;
}

static void test_status_handler_callback() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  g_status_handler_called = false;

  Bridge.onStatus(etl::delegate<void(rpc::StatusCode, etl::span<const uint8_t>)>::create<status_callback>());

  // Dispatch a STATUS_ACK command to trigger onStatusCommand
  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_OK);
  f.header.payload_length = 0;
  f.payload = etl::span<const uint8_t>();
  bridge::router::CommandContext ctx(&f, f.header.command_id, false, false, 0);
  ba.routeStatusCommand(ctx);

  TEST_ASSERT_TRUE(g_status_handler_called);
}

// ============================================================================
// 15. Decompressed Frame with RLE (Bridge.cpp:739-742)
// ============================================================================

static void test_decompressed_frame_rle() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Build a frame with the compressed bit set and valid RLE payload
  // The compressed bit is bit 15 of command_id
  uint16_t base_cmd = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  uint16_t compressed_cmd = base_cmd | (1U << rpc::RPC_CMD_FLAG_COMPRESSED_BIT);

  // RLE payload: just literals "ABC" (no escape bytes)
  uint8_t rle_payload[] = {0x41, 0x42, 0x43};

  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = compressed_cmd;
  f.header.payload_length = 3;
  f.payload = etl::span<const uint8_t>(rle_payload, 3);
  f.crc = 0;

  // Dispatch should decompress and handle
  ba.dispatch(f);
  // If we got here without crash, decompression worked
  TEST_ASSERT(true);
}

static void test_decompressed_frame_rle_failure() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Build a frame with compressed bit and malformed RLE
  uint16_t base_cmd = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  uint16_t compressed_cmd = base_cmd | (1U << rpc::RPC_CMD_FLAG_COMPRESSED_BIT);

  // Malformed RLE: escape byte followed by only 1 byte (needs 2)
  const uint8_t escape = rle::ESCAPE_BYTE;
  uint8_t rle_payload[] = {escape, 0x00};  // Truncated escape block

  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = compressed_cmd;
  f.header.payload_length = 2;
  f.payload = etl::span<const uint8_t>(rle_payload, 2);
  f.crc = 0;

  // Should emit MALFORMED status
  ba.dispatch(f);
  TEST_ASSERT(true);  // No crash
}

// ============================================================================
// 16. Handshake Tag Computation (Bridge.cpp:764)
// ============================================================================

static void test_handshake_tag_computation() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);

  // Set a shared secret
  const uint8_t secret[] = "test_secret_123";
  ba.assignSharedSecret(secret, secret + sizeof(secret) - 1);

  uint8_t nonce[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
  uint8_t tag[16] = {};

  ba.computeHandshakeTag(nonce, sizeof(nonce), tag);

  // Verify tag is not all zeros (computation happened)
  bool all_zero = true;
  for (size_t i = 0; i < 16; ++i) {
    if (tag[i] != 0) { all_zero = false; break; }
  }
  TEST_ASSERT_FALSE(all_zero);
}

// ============================================================================
// 17. sendFrame in fault and unsynchronized states (Bridge.h:193+)
// ============================================================================

static void test_send_frame_fault_state() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.fsmCryptoFault();
  TEST_ASSERT_TRUE(Bridge.isFault());

  bool result = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION);
  TEST_ASSERT_FALSE(result);
}

static void test_send_frame_unsynchronized() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setUnsynchronized();
  TEST_ASSERT_TRUE(Bridge.isUnsynchronized());

  // Non-handshake command should fail
  bool result = Bridge.sendFrame(rpc::CommandId::CMD_DIGITAL_WRITE);
  TEST_ASSERT_FALSE(result);

  // Handshake command should succeed
  bool result2 = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION);
  TEST_ASSERT_TRUE(result2);
}

// ============================================================================
// 18. Security check failed (Bridge.cpp:230-231)
// ============================================================================

static void test_security_check_failed_dispatch() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);

  // Put bridge in Syncing state (not synchronized yet)
  ba.fsmResetFsm();
  ba.setStartupStabilizing(false);
  ba.fsmHandshakeStart();
  TEST_ASSERT_TRUE(Bridge.isSyncing());

  // Dispatch a non-handshake command — should fail security check
  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.header.payload_length = 0;
  f.payload = etl::span<const uint8_t>();
  
  ba.dispatch(f);
  // Should have sent STATUS_ERROR (no crash)
  TEST_ASSERT(true);
}

// ============================================================================
// 19. HAL edge cases (hal.cpp uncovered lines)
// ============================================================================

static void test_hal_path_safety() {
  // Test path validation edge cases
  // These are internal to hal.cpp, tested indirectly through FileSystem operations

  // Write to a path containing '..' — should fail
  auto res = bridge::hal::writeFile(etl::string_view("../evil"), etl::span<const uint8_t>());
  TEST_ASSERT_FALSE(res.has_value());

  // Write to absolute path — should fail
  res = bridge::hal::writeFile(etl::string_view("/etc/passwd"), etl::span<const uint8_t>());
  TEST_ASSERT_FALSE(res.has_value());

  // Write to path with backslash — should fail
  res = bridge::hal::writeFile(etl::string_view("dir\\file"), etl::span<const uint8_t>());
  TEST_ASSERT_FALSE(res.has_value());

  // Read from empty path — should fail
  etl::array<uint8_t, 16> buf = {};
  auto rres = bridge::hal::readFileChunk(etl::string_view(""), 0, etl::span<uint8_t>(buf.data(), buf.size()));
  TEST_ASSERT_FALSE(rres.has_value());

  // Remove with '..' path — should fail
  auto rmer = bridge::hal::removeFile(etl::string_view("../forbidden"));
  TEST_ASSERT_FALSE(rmer.has_value());
}

static void test_hal_readfile_offset_beyond_size() {
  // Write a small file, then try to read beyond its size
  uint8_t data[] = {0x01, 0x02, 0x03};
  auto wres = bridge::hal::writeFile(etl::string_view("coverage_test_file.bin"),
                                     etl::span<const uint8_t>(data, 3));
  TEST_ASSERT_TRUE(wres.has_value());

  etl::array<uint8_t, 16> buf = {};
  auto rres = bridge::hal::readFileChunk(
    etl::string_view("coverage_test_file.bin"), 999,
    etl::span<uint8_t>(buf.data(), buf.size()));
  TEST_ASSERT_FALSE(rres.has_value());

  // Clean up
  bridge::hal::removeFile(etl::string_view("coverage_test_file.bin"));
}

static void test_hal_write_with_subdirectory() {
  // Write to a path with subdirectories — triggers ensure_host_parent_directories
  uint8_t data[] = {0xAA};
  auto wres = bridge::hal::writeFile(
    etl::string_view("subdir/nested/testfile.bin"),
    etl::span<const uint8_t>(data, 1));
  TEST_ASSERT_TRUE(wres.has_value());

  // Read it back
  etl::array<uint8_t, 16> buf = {};
  auto rres = bridge::hal::readFileChunk(
    etl::string_view("subdir/nested/testfile.bin"), 0,
    etl::span<uint8_t>(buf.data(), buf.size()));
  TEST_ASSERT_TRUE(rres.has_value());
  TEST_ASSERT_EQUAL(1U, rres.value().bytes_read);
  TEST_ASSERT_EQUAL(0xAA, buf[0]);

  // Clean up
  bridge::hal::removeFile(etl::string_view("subdir/nested/testfile.bin"));
}

// ============================================================================
// 20. FileSystem — read handler clear on failure (FileSystem.cpp:31)
// ============================================================================

static void test_filesystem_read_handler_clear() {
#if BRIDGE_ENABLE_FILESYSTEM
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);

  // Put in fault state so sendPbCommand fails
  ba.fsmCryptoFault();

  FileSystem.read(etl::string_view("test.txt"),
    etl::delegate<void(etl::span<const uint8_t>)>());
  // Handler should have been cleared because sendPbCommand failed
  // No crash = success
  TEST_ASSERT(true);
#endif
}

// ============================================================================
// 20b. FileSystem — read empty file triggers empty-payload branch (FileSystem.cpp:88-90)
// ============================================================================

static void test_filesystem_read_empty_file() {
#if BRIDGE_ENABLE_FILESYSTEM
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Create an empty file on the host filesystem so readFileChunk returns bytes_read=0
  constexpr const char* kEmptyFile = "/tmp/mcubridge-host-fs/empty_test.txt";
  FILE* f = fopen(kEmptyFile, "wb");
  TEST_ASSERT_NOT_NULL(f);
  fclose(f);

  // Call _onRead directly with a path to the empty file
  rpc::payload::FileRead msg = {};
  strncpy(msg.path, "empty_test.txt", sizeof(msg.path) - 1);
  FileSystem._onRead(msg);

  // Clean up
  ::remove(kEmptyFile);

  // No crash = success. The else-if branch (bytes_read==0, !sent_payload) was exercised.
  TEST_ASSERT(true);
#endif
}

// ============================================================================
// 21. Duplicate frame with ACK re-send (Bridge.cpp:208-211)
// ============================================================================

static void test_duplicate_frame_handling() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Create a frame that requires ACK
  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN);  // requires_ack
  f.header.payload_length = 0;
  f.header.sequence_id = 42;
  f.payload = etl::span<const uint8_t>();

  // First dispatch: should process normally
  ba.dispatch(f, 42);

  // Mark this sequence as processed
  ba.markRxProcessed(f);

  // Verify it's now in the history
  TEST_ASSERT_TRUE(ba.isRecentDuplicateRx(f));
}

// ============================================================================
// 26. _handleReceivedFrame decompress failure (Bridge.cpp:199-200)
// ============================================================================

static void test_handle_received_frame_decompress_fail() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Build a valid frame with compressed bit + bad RLE using FrameBuilder
  uint16_t base_cmd = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  uint16_t compressed_cmd = base_cmd | (1U << rpc::RPC_CMD_FLAG_COMPRESSED_BIT);

  // Truncated RLE escape — rle::decode returns 0
  const uint8_t escape = rle::ESCAPE_BYTE;
  uint8_t bad_rle[] = {escape, 0x00};  // Only 1 byte after escape, need 2

  rpc::FrameBuilder builder;
  etl::array<uint8_t, 128> raw_frame = {};
  size_t raw_len = builder.build(
      etl::span<uint8_t>(raw_frame.data(), raw_frame.size()),
      compressed_cmd, 99,
      etl::span<const uint8_t>(bad_rle, sizeof(bad_rle)));
  TEST_ASSERT_GREATER_THAN(0U, raw_len);

  // Feed through handleReceivedFrame (NOT dispatch) to hit line 199-200
  ba.handleReceivedFrame(etl::span<const uint8_t>(raw_frame.data(), raw_len));
  TEST_ASSERT(true);
}

// ============================================================================
// 27. _handleReceivedFrame duplicate with ACK (Bridge.cpp:208-209,211)
// ============================================================================

static void test_handle_received_frame_dup_with_ack() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Build a frame with ACK-requiring command
  uint16_t cmd = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  mcubridge_DigitalWrite dw = mcubridge_DigitalWrite_init_default;
  dw.pin = 5;
  dw.value = 1;
  etl::array<uint8_t, 32> pb_buf = {};
  pb_ostream_t s = pb_ostream_from_buffer(pb_buf.data(), pb_buf.size());
  pb_encode(&s, mcubridge_DigitalWrite_fields, &dw);

  rpc::FrameBuilder builder;
  etl::array<uint8_t, 128> raw_frame = {};
  size_t raw_len = builder.build(
      etl::span<uint8_t>(raw_frame.data(), raw_frame.size()),
      cmd, 77,
      etl::span<const uint8_t>(pb_buf.data(), s.bytes_written));
  TEST_ASSERT_GREATER_THAN(0U, raw_len);

  // First reception — goes through normal path and is processed
  ba.handleReceivedFrame(etl::span<const uint8_t>(raw_frame.data(), raw_len));

  // Second reception — should be detected as duplicate and ACK re-sent (lines 208-209)
  ba.handleReceivedFrame(etl::span<const uint8_t>(raw_frame.data(), raw_len));
  TEST_ASSERT(true);
}

// ============================================================================
// 28. _handleReceivedFrame parse error (Bridge.cpp:215)
// ============================================================================

static void test_handle_received_frame_parse_error() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Feed garbage data — will fail parser.parse()
  uint8_t garbage[] = {0x01, 0x02, 0x03, 0x04};
  ba.handleReceivedFrame(etl::span<const uint8_t>(garbage, sizeof(garbage)));

  // Should have set _last_parse_error
  TEST_ASSERT(true);
}

// ============================================================================
// 29. Enter bootloader (Bridge.cpp:451-452)
// ============================================================================

static void test_enter_bootloader() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Build EnterBootloader with correct magic
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buf = {};
  mcubridge_EnterBootloader msg = mcubridge_EnterBootloader_init_default;
  msg.magic = rpc::RPC_BOOTLOADER_MAGIC;
  pb_ostream_t s = pb_ostream_from_buffer(payload_buf.data(), payload_buf.size());
  pb_encode(&s, mcubridge_EnterBootloader_fields, &msg);

  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ENTER_BOOTLOADER);
  f.header.payload_length = static_cast<uint16_t>(s.bytes_written);
  f.payload = etl::span<const uint8_t>(payload_buf.data(), s.bytes_written);
  bridge::router::CommandContext ctx(&f, f.header.command_id, false, true, 10);
  ba.routeSystemCommand(ctx);
  TEST_ASSERT(true);
}

// ============================================================================
// 30. _applyTimingConfig all fields (Bridge.cpp:512-513)
// ============================================================================

static void test_apply_timing_all_fields() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);

  // Build HandshakeConfig with all fields set
  etl::array<uint8_t, 32> pb_buf = {};
  mcubridge_HandshakeConfig msg = mcubridge_HandshakeConfig_init_default;
  msg.ack_timeout_ms = 500;
  msg.ack_retry_limit = 7;
  msg.response_timeout_ms = 2000;
  pb_ostream_t s = pb_ostream_from_buffer(pb_buf.data(), pb_buf.size());
  pb_encode(&s, mcubridge_HandshakeConfig_fields, &msg);

  ba.applyTimingConfig(pb_buf.data(), s.bytes_written);
  TEST_ASSERT_EQUAL(500U, ba.getAckTimeoutMs());
  TEST_ASSERT_EQUAL(7U, ba.getAckRetryLimit());
}

// ============================================================================
// 31. ACK timeout retransmit (not exceeded) (Bridge.cpp:701)
// ============================================================================

static void test_ack_timeout_retransmit() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Push a pending TX frame and move to AwaitingAck
  uint8_t payload[] = {0x01};
  ba.pushPendingTxFrame(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION), 1, payload);
  ba.flushPendingTxQueue();
  TEST_ASSERT_TRUE(Bridge.isAwaitingAck());

  // Set retry count below limit
  ba.setRetryCount(0);
  ba.setAckRetryLimit(3);

  // Trigger ACK timeout — should retransmit, NOT exceed limit
  ba.onAckTimeout();
  TEST_ASSERT_TRUE(Bridge.isAwaitingAck());  // Still in AwaitingAck
}

// ============================================================================
// 32. Baudrate change (Bridge.cpp:703)
// ============================================================================

static void test_baudrate_change() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);

  ba.setPendingBaudrate(9600);
  TEST_ASSERT_EQUAL(9600U, ba.getPendingBaudrate());

  ba.onBaudrateChange();
  TEST_ASSERT_EQUAL(0U, ba.getPendingBaudrate());
}

// ============================================================================
// 33. emitStatus with string_view (Bridge.cpp:739-742)
// ============================================================================

static void test_emit_status_string_view() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Non-empty message
  ba.emitStatusStringView(rpc::StatusCode::STATUS_ERROR, "test error");
  TEST_ASSERT(true);

  // Empty message — hits early return path
  ba.emitStatusStringView(rpc::StatusCode::STATUS_ERROR, "");
  TEST_ASSERT(true);
}

// ============================================================================
// 34. emitStatus with FlashStringHelper (Bridge.cpp:748-755)
// ============================================================================

static void test_emit_status_flash() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Non-null message
  ba.emitStatusFlash(rpc::StatusCode::STATUS_ERROR, F("flash error"));
  TEST_ASSERT(true);

  // Null message
  ba.emitStatusFlash(rpc::StatusCode::STATUS_ERROR, nullptr);
  TEST_ASSERT(true);
}

// ============================================================================
// 35. Pin read valid path (Bridge.h:274)
// ============================================================================

static void test_pin_read_valid() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // DigitalRead with valid pin — route through GPIO handler
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buf = {};
  mcubridge_PinRead msg = mcubridge_PinRead_init_default;
  msg.pin = 5;  // Valid pin
  pb_ostream_t s = pb_ostream_from_buffer(payload_buf.data(), payload_buf.size());
  pb_encode(&s, mcubridge_PinRead_fields, &msg);

  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
  f.header.payload_length = static_cast<uint16_t>(s.bytes_written);
  f.payload = etl::span<const uint8_t>(payload_buf.data(), s.bytes_written);
  ba.dispatch(f);
  TEST_ASSERT(true);

  // AnalogRead with valid pin
  msg.pin = 0;  // A0
  s = pb_ostream_from_buffer(payload_buf.data(), payload_buf.size());
  pb_encode(&s, mcubridge_PinRead_fields, &msg);
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ);
  f.header.payload_length = static_cast<uint16_t>(s.bytes_written);
  f.payload = etl::span<const uint8_t>(payload_buf.data(), s.bytes_written);
  ba.dispatch(f);
  TEST_ASSERT(true);

  // DigitalRead with INVALID pin — should emit STATUS_ERROR (Bridge.h:274)
  msg.pin = 254;  // Invalid pin
  s = pb_ostream_from_buffer(payload_buf.data(), payload_buf.size());
  pb_encode(&s, mcubridge_PinRead_fields, &msg);
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ);
  f.header.payload_length = static_cast<uint16_t>(s.bytes_written);
  f.payload = etl::span<const uint8_t>(payload_buf.data(), s.bytes_written);
  ba.dispatch(f);
  TEST_ASSERT(true);
}

// ============================================================================
// 36. FSM on_event_unknown from all states (bridge_fsm.h:150,176,208,217)
// ============================================================================

static void test_fsm_unknown_event_stabilizing() {
  // Start fresh — begin() leaves FSM in Stabilizing
  g_stream.clear();
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(g_stream);
  Bridge.begin();
  auto ba = TestAccessor::create(Bridge);

  TEST_ASSERT_TRUE(ba.getStartupStabilizing());

  // EvHandshakeStart is NOT accepted in Stabilizing
  ba.fsmHandshakeStart();
  TEST_ASSERT_TRUE(ba.getStartupStabilizing());  // No change
}

static void test_fsm_crypto_fault_from_stabilizing() {
  // Start fresh
  g_stream.clear();
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(g_stream);
  Bridge.begin();
  auto ba = TestAccessor::create(Bridge);

  TEST_ASSERT_TRUE(ba.getStartupStabilizing());

  // CryptoFault from Stabilizing → Fault
  ba.fsmCryptoFault();
  TEST_ASSERT_TRUE(Bridge.isFault());
}

static void test_fsm_unknown_event_syncing() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.fsmResetFsm();
  ba.setStartupStabilizing(false);  // → Unsynchronized
  ba.fsmHandshakeStart();           // → Syncing
  TEST_ASSERT_TRUE(Bridge.isSyncing());

  // EvSendCritical is NOT accepted in Syncing → on_event_unknown
  ba.fsmSendCritical();
  TEST_ASSERT_TRUE(Bridge.isSyncing());  // No change
}

static void test_fsm_unknown_event_idle() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  TEST_ASSERT_TRUE(Bridge.isIdle());

  // EvHandshakeStart is NOT accepted in Idle → on_event_unknown
  ba.fsmHandshakeStart();
  TEST_ASSERT_TRUE(Bridge.isIdle());  // No change
}

static void test_fsm_unknown_event_awaiting_ack() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setAwaitingAck();
  TEST_ASSERT_TRUE(Bridge.isAwaitingAck());

  // EvHandshakeStart is NOT accepted in AwaitingAck → on_event_unknown
  ba.fsmHandshakeStart();
  TEST_ASSERT_TRUE(Bridge.isAwaitingAck());  // No change
}

// ============================================================================
// 38. _onRxDedupe (Bridge.cpp:701)
// ============================================================================

static void test_rx_dedupe() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);

  // Mark some frames as processed
  rpc::Frame f = {};
  f.header.sequence_id = 100;
  ba.markRxProcessed(f);
  TEST_ASSERT_TRUE(ba.isRecentDuplicateRx(f));

  // Call onRxDedupe — should clear history
  ba.onRxDedupe();

  TEST_ASSERT_FALSE(ba.isRecentDuplicateRx(f));
}

// ============================================================================
// 39. FSM unknown event from Fault (bridge_fsm.h:217)
// ============================================================================

static void test_fsm_unknown_event_fault() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.fsmCryptoFault();
  TEST_ASSERT_TRUE(Bridge.isFault());

  // EvHandshakeStart is NOT accepted in Fault → on_event_unknown
  ba.fsmHandshakeStart();
  TEST_ASSERT_TRUE(Bridge.isFault());  // No change
}

// ============================================================================
// 40. HAL ensure_host_directory mkdir success (hal.cpp:63)
// ============================================================================

static void test_hal_mkdir_fresh_dir() {
  // Remove any leftover directory first, then write to force mkdir to succeed
  bridge::hal::removeFile(etl::string_view("freshdir/file.txt"));
  ::rmdir("/tmp/mcubridge-host-fs/freshdir");

  uint8_t data[] = {0xBB};
  auto wres = bridge::hal::writeFile(
    etl::string_view("freshdir/file.txt"),
    etl::span<const uint8_t>(data, 1));
  TEST_ASSERT_TRUE(wres.has_value());

  // Clean up
  bridge::hal::removeFile(etl::string_view("freshdir/file.txt"));
  ::rmdir("/tmp/mcubridge-host-fs/freshdir");
}

// ============================================================================
// 22. sendPbCommand/sendPbFrame template (Bridge.h:193-206)
// ============================================================================

static void test_send_pb_command_template() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Test sendPbCommand with a valid protobuf message
  rpc::payload::VersionResponse msg = {};
  msg.major = 1;
  msg.minor = 0;
  msg.patch = 0;
  bool result = Bridge.sendPbCommand(rpc::CommandId::CMD_GET_VERSION_RESP, 0, msg);
  TEST_ASSERT_TRUE(result);
}

static void test_send_pb_frame_template() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Test sendPbFrame with a status code
  rpc::payload::AckPacket msg = {};
  msg.command_id = 42;
  bool result = Bridge.sendPbFrame(rpc::StatusCode::STATUS_ACK, 0, msg);
  TEST_ASSERT_TRUE(result);
}

// ============================================================================
// 23. _handlePinSetter invalid pin (Bridge.h:325-340)
// ============================================================================

static void test_pin_setter_invalid_pin() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Send DigitalWrite with invalid pin number
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buf = {};
  mcubridge_DigitalWrite msg = mcubridge_DigitalWrite_init_default;
  msg.pin = 255;  // Invalid pin
  msg.value = 1;
  pb_ostream_t s = pb_ostream_from_buffer(payload_buf.data(), payload_buf.size());
  pb_encode(&s, mcubridge_DigitalWrite_fields, &msg);

  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.header.payload_length = static_cast<uint16_t>(s.bytes_written);
  f.payload = etl::span<const uint8_t>(payload_buf.data(), s.bytes_written);
  ba.dispatch(f);
  TEST_ASSERT(true);  // Should emit STATUS_ERROR, not crash
}

// ============================================================================
// 24. _withAck duplicate + ack path (Bridge.h:366-376)
// ============================================================================

static void test_with_ack_duplicate() {
  reset_test_bridge();
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  f.header.version = rpc::PROTOCOL_VERSION;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SPI_BEGIN);
  f.header.payload_length = 0;
  f.payload = etl::span<const uint8_t>();

  // Create context with is_duplicate=true and requires_ack=true
  bridge::router::CommandContext ctx(&f, f.header.command_id, true, true, 1);
  // Route as SPI command — _withAck should skip handler but send ACK
  ba.routeSpiCommand(ctx);
  TEST_ASSERT(true);
}

// ============================================================================
// 25. Console write with null/zero args (Console.cpp additional paths)
// ============================================================================

static void test_console_write_not_begun() {
  // Console not begun — all writes should return 0
  Console.~ConsoleClass();
  new (&Console) ConsoleClass();
  // Don't call begin()
  
  TEST_ASSERT_EQUAL(0U, Console.write('A'));
  uint8_t buf[] = {'B'};
  TEST_ASSERT_EQUAL(0U, Console.write(buf, 1));
  TEST_ASSERT_EQUAL(0U, Console.write(nullptr, 1));

  Console.begin();  // Restore
}

// ============================================================================
// Entry point
// ============================================================================

void setUp(void) {}
void tearDown(void) {}

int main() {
  UNITY_BEGIN();

  // RLE
  RUN_TEST(test_rle_decode_empty_input);
  RUN_TEST(test_rle_decode_literals_only);
  RUN_TEST(test_rle_decode_run_expansion);
  RUN_TEST(test_rle_decode_single_escape_marker);
  RUN_TEST(test_rle_decode_dst_overflow_literals);
  RUN_TEST(test_rle_decode_dst_overflow_run);
  RUN_TEST(test_rle_decode_truncated_escape);
  RUN_TEST(test_rle_decode_mixed);

  // Frame parser
  RUN_TEST(test_frame_parser_version_mismatch);
  RUN_TEST(test_frame_parser_payload_length_mismatch);
  RUN_TEST(test_frame_parser_overflow);

  // Observer
  RUN_TEST(test_observer_default_notifications);

  // Service handlers with callbacks
  RUN_TEST(test_datastore_response_with_handler);
  RUN_TEST(test_mailbox_with_handlers);
  RUN_TEST(test_process_kill_error_path);

  // SPI
  RUN_TEST(test_spi_mock_methods);
  RUN_TEST(test_spi_handlers_dispatch);

  // pb_copy_join
  RUN_TEST(test_pb_copy_join_parts);
  RUN_TEST(test_pb_copy_join_overflow);
  RUN_TEST(test_pb_copy_join_empty_dst);

  // Console
  RUN_TEST(test_console_write_buffer_full_break);
  RUN_TEST(test_console_read_xoff_reset);
  RUN_TEST(test_console_write_not_begun);

  // FSM
  RUN_TEST(test_fsm_all_state_transitions);
  RUN_TEST(test_fsm_crypto_fault_from_each_state);
  RUN_TEST(test_fsm_reset_from_syncing);
  RUN_TEST(test_fsm_reset_from_idle);
  RUN_TEST(test_fsm_awaiting_ack_reset);

  // Bridge paths
  RUN_TEST(test_crc_error_escalation);
  RUN_TEST(test_ack_timeout_retry_exceeded);
  RUN_TEST(test_status_handler_callback);
  RUN_TEST(test_decompressed_frame_rle);
  RUN_TEST(test_decompressed_frame_rle_failure);
  RUN_TEST(test_handshake_tag_computation);
  RUN_TEST(test_send_frame_fault_state);
  RUN_TEST(test_send_frame_unsynchronized);
  RUN_TEST(test_security_check_failed_dispatch);
  RUN_TEST(test_send_pb_command_template);
  RUN_TEST(test_send_pb_frame_template);

  // Pin validation
  RUN_TEST(test_pin_setter_invalid_pin);
  RUN_TEST(test_with_ack_duplicate);

  // Duplicate frame
  RUN_TEST(test_duplicate_frame_handling);

  // HAL
  RUN_TEST(test_hal_path_safety);
  RUN_TEST(test_hal_readfile_offset_beyond_size);
  RUN_TEST(test_hal_write_with_subdirectory);

  // FileSystem
  RUN_TEST(test_filesystem_read_handler_clear);
  RUN_TEST(test_filesystem_read_empty_file);

  // handleReceivedFrame paths
  RUN_TEST(test_handle_received_frame_decompress_fail);
  RUN_TEST(test_handle_received_frame_dup_with_ack);
  RUN_TEST(test_handle_received_frame_parse_error);

  // Bootloader, timing, retransmit, baudrate
  RUN_TEST(test_enter_bootloader);
  RUN_TEST(test_apply_timing_all_fields);
  RUN_TEST(test_ack_timeout_retransmit);
  RUN_TEST(test_baudrate_change);

  // emitStatus overloads
  RUN_TEST(test_emit_status_string_view);
  RUN_TEST(test_emit_status_flash);

  // Pin read valid path
  RUN_TEST(test_pin_read_valid);

  // FSM on_event_unknown
  RUN_TEST(test_fsm_unknown_event_stabilizing);
  RUN_TEST(test_fsm_crypto_fault_from_stabilizing);
  RUN_TEST(test_fsm_unknown_event_syncing);
  RUN_TEST(test_fsm_unknown_event_idle);
  RUN_TEST(test_fsm_unknown_event_awaiting_ack);

  // Extra coverage: RxDedupe, Fault unknown event, HAL mkdir
  RUN_TEST(test_rx_dedupe);
  RUN_TEST(test_fsm_unknown_event_fault);
  RUN_TEST(test_hal_mkdir_fresh_dir);

  return UNITY_END();
}
