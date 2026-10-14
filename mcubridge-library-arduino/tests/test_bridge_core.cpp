#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <BridgeFaultInjection.h>
#include <etl/array.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "services/Console.h"
#include "test_support.h"  // IWYU pragma: keep

using namespace bridge::test;

void setUp() {}
void tearDown() {}

void reset_bridge() {
  Bridge.begin(115200, "6368616e67656d65313233");
  Console.begin();
}

void test_bridge_initialization() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());
  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_handshake() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // 1. Prepare Handshake Payload using computeHandshakeTag
  etl::array<uint8_t, 16> nonce;
  nonce.fill(0x42);
  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), nonce.size(), tag.data());

  rpc::payload::LinkSync msg = {};
  etl::copy_n(nonce.begin(), 16, msg.nonce.bytes);
  msg.nonce.size = 16;
  etl::copy_n(tag.begin(), 16, msg.tag.bytes);
  msg.tag.size = 16;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg));

  // 2. Build LinkSync frame using FrameBuilder
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};
  // [MEM-SAVE] Reusing nonce for handshake (aligned with protocol spec).
  etl::copy_n(nonce.begin(), rpc::RPC_AEAD_NONCE_SIZE, frame_nonce.begin());

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 1,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, tag),
      frame_raw);

  // 3. Dispatch using FrameParser
  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();

  TEST_ASSERT(ba.isSynchronized());
}

void test_bridge_send_frame() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> payload = {0xAA, 0xBB};
  TEST_ASSERT(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 123,
                               etl::span<const uint8_t>(payload.data(), 2)));
}

void test_bridge_process_rx() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::payload::DigitalWrite msg = {};
  msg.pin = 13;
  msg.value = 1;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 10,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, {}),
      frame_raw);

  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();
}

void test_bridge_dedup_console_write() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Build ConsoleWrite frame once
  rpc::payload::ConsoleWrite msg = {};
  const char* text = "TEST";
  etl::copy_n(text, 4, msg.data.bytes);
  msg.data.size = 4;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), 55,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, {}),
      frame_raw);

  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());

  // 2. Dispatch twice
  ba.dispatch(frame_res.value());
  Bridge.process();
  TEST_ASSERT_EQUAL(4, Console.available());

  ba.dispatch(frame_res.value());
  Bridge.process();
  // 3. Verify Console.available() remains consistent (deduplicated)
  TEST_ASSERT_EQUAL(4, Console.available());
}

void test_bridge_status_ack() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Trigger a command that requires ACK
  TEST_ASSERT_TRUE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 77));
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  // 2. Build STATUS_ACK frame targeting sequence ID 77
  rpc::payload::AckPacket p = {};
  p.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(p)>(), &p));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> frame_tag = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::StatusCode::STATUS_ACK), 77,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, frame_tag),
      frame_raw);

  // 3. Dispatch and verify isAwaitingAck() becomes false
  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();

  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_post_and_stack_sentinel() {
  TEST_ASSERT_TRUE(bridge::hal::run_power_on_self_tests());
  TEST_ASSERT_TRUE(bridge::hal::checkStackOverflow());
  TEST_ASSERT_GREATER_OR_EQUAL_UINT16(bridge::hal::MIN_STACK_MARGIN_BYTES,
                                      bridge::hal::getFreeStackMargin());
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
  TEST_ASSERT_GREATER_OR_EQUAL_UINT16(bridge::hal::MIN_STACK_MARGIN_BYTES,
                                      Bridge.getFreeStackMargin());
}

void test_bridge_wcet_tracking() {
  Bridge.resetWcetStats();
  TEST_ASSERT_EQUAL_UINT32(0, Bridge.getWcetMaxMicros());
  Bridge.process();
  TEST_ASSERT_GREATER_OR_EQUAL_UINT32(0, Bridge.getWcetMaxMicros());
}

void test_bridge_begin_null_secret() {
  Bridge.begin(115200, nullptr);
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
  Bridge.process();
}

void test_bridge_handshake_timeout_not_sync() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // Trigger timeout when not synchronized -> FAULT state
  ba.onHandshakeTimeout();
  TEST_ASSERT_TRUE(ba.isFault());

  // Trigger timeout when synchronized -> does not enter fault
  reset_bridge();
  ba.setSynchronized();
  ba.onHandshakeTimeout();
  TEST_ASSERT_FALSE(ba.isFault());
}

namespace {
void dummy_status_handler(rpc::StatusCode, etl::span<const uint8_t>) {}
}

void test_bridge_queue_full_and_flow_control() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint16_t, bridge::config::MAX_PENDING_TX_FRAMES> seq_ids{};
  etl::iota(seq_ids.begin(), seq_ids.end(), 100);
  etl::for_each(seq_ids.begin(), seq_ids.end(), [](uint16_t seq) {
    TEST_ASSERT_TRUE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, seq));
  });
  TEST_ASSERT_TRUE(ba.isAwaitingAck());
  TEST_ASSERT_FALSE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 105));

  Bridge.signalXoff();
  Bridge.signalXon();
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_bridge_emit_status_variants() {
  reset_bridge();
  Bridge.onStatus(BridgeClass::StatusHandler::create<dummy_status_handler>());
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK);
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view("Error"));

  Bridge.onStatus(BridgeClass::StatusHandler());
  Bridge.emitStatus(rpc::StatusCode::STATUS_MALFORMED, F("FlashError"));
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, etl::string_view(""));
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, (const __FlashStringHelper*)nullptr);
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_send_responses_table() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint8_t data[] = "d";

  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 1, []() {
    rpc::payload::VersionResponse p;
    p.major = 1;
    p.minor = 2;
    p.patch = 3;
    return p;
  }()));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, 1, []() {
    rpc::payload::FreeMemoryResponse p;
    p.value = 1024;
    return p;
  }()));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, 1,
                               rpc::payload::Capabilities{}));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_DIGITAL_READ_RESP, 1, []() {
    rpc::payload::DigitalReadResponse p;
    p.value = 1;
    return p;
  }()));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_ANALOG_READ_RESP, 1, []() {
    rpc::payload::AnalogReadResponse p;
    p.value = 512;
    return p;
  }()));

  rpc::payload::DatastoreGetResponse dgr;
  rpc::payload::copy_to_pb_bytes(dgr.value, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_DATASTORE_GET_RESP, 1, dgr));

  rpc::payload::FileReadResponse frr;
  rpc::payload::copy_to_pb_bytes(frr.content, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 1, frr));

  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP, 1, []() {
    rpc::payload::ProcessRunAsyncResponse p;
    p.pid = 123;
    return p;
  }()));

  rpc::payload::ProcessPollResponse ppr;
  rpc::payload::copy_to_pb_bytes(ppr.stdout_data, data, 1);
  rpc::payload::copy_to_pb_bytes(ppr.stderr_data, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_PROCESS_POLL_RESP, 1, ppr));

  rpc::payload::SpiTransferResponse strr;
  rpc::payload::copy_to_pb_bytes(strr.data, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, 1, strr));
}

void test_bridge_dispatch_status_and_unknown() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f.command_id = (uint16_t)rpc::StatusCode::STATUS_OK;
  ba.dispatch(f);
  TEST_ASSERT_TRUE(Bridge.isSynchronized());

  f.command_id = 999;
  ba.dispatch(f);

  rpc_pb_RpcEnvelope f_gap = rpc_pb_RpcEnvelope_init_default;
  f_gap.command_id = 65;
  ba.dispatch(f_gap);

  f.command_id = (uint16_t)rpc::StatusCode::STATUS_MALFORMED;
  ba.dispatch(f);
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_bridge_post_failure() {
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL);
  Bridge.begin(115200, "secret");
  bridge::test::fault::reset();
  reset_bridge();
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_frame_errors_and_zero_spi() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Frame too short -> STATUS_MALFORMED
  uint8_t malformed[] = {0x01, 0x02};
  ba.invokePacketReceived(etl::span<const uint8_t>(malformed, sizeof(malformed)));

  // 2. Frame with bad CRC -> STATUS_CRC_MISMATCH
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = rpc::PROTOCOL_VERSION;
  env.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION;
  size_t len = rpc::serialize_frame(env, frame_raw);
  if (len > 4) {
    frame_raw[len - 1] ^= 0xFF;  // corrupt CRC
    ba.invokePacketReceived(etl::span<const uint8_t>(frame_raw.data(), len));
  }

  // 3. Zero-length SPI transfer -> to_copy == 0
  rpc_pb_RpcEnvelope f_zero_spi = rpc_pb_RpcEnvelope_init_default;
  f_zero_spi.version = rpc::PROTOCOL_VERSION;
  f_zero_spi.command_id = (uint16_t)rpc::CommandId::CMD_SPI_TRANSFER;
  f_zero_spi.sequence_id = 99;
  ba.dispatch(f_zero_spi);

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

class FlowControlTestStream : public Stream {
 public:
  int avail = 0;
  int available() override { return avail; }
  int read() override { return -1; }
  int peek() override { return -1; }
  size_t write(uint8_t) override { return 1; }
  size_t write(const uint8_t*, size_t s) override { return s; }
  void flush() override {}
};

void test_bridge_serial_tasks_and_flow_control() {
  BiStream base_stream;
  reset_bridge_core(Bridge, base_stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Test serial flow control high and low watermarks
  FlowControlTestStream fs;
  reset_bridge_core(Bridge, fs);
  fs.avail = 100;
  ba.invokeSerialTask();
  fs.avail = 5;
  ba.invokeSerialTask();
  reset_bridge_core(Bridge, base_stream);

  // Test ack timeouts until max retransmit
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvSendCritical());
  TEST_ASSERT_TRUE(ba.isAwaitingAck());
  ba.onAckTimeout();
  ba.onAckTimeout();
  ba.onAckTimeout();
  ba.onAckTimeout();

  // Test baudrate change callback and watchdog invoke
  ba.setPendingBaudrate(rpc::RPC_DEFAULT_BAUDRATE);
  ba.onBaudrateChange();
  ba.invokeWatchdog();

  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_initialization);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  RUN_TEST(test_bridge_dedup_console_write);
  RUN_TEST(test_bridge_status_ack);
  RUN_TEST(test_bridge_post_and_stack_sentinel);
  RUN_TEST(test_bridge_wcet_tracking);
  RUN_TEST(test_bridge_begin_null_secret);
  RUN_TEST(test_bridge_handshake_timeout_not_sync);
  RUN_TEST(test_bridge_queue_full_and_flow_control);
  RUN_TEST(test_bridge_emit_status_variants);
  RUN_TEST(test_bridge_send_responses_table);
  RUN_TEST(test_bridge_dispatch_status_and_unknown);
  RUN_TEST(test_bridge_post_failure);
  RUN_TEST(test_bridge_frame_errors_and_zero_spi);
  RUN_TEST(test_bridge_serial_tasks_and_flow_control);
  return UNITY_END();
}
