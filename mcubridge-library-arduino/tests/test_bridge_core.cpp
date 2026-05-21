#include <etl/array.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "services/Console.h"
#include "test_constants.h"
#include "test_support.h"

// Define the global delegates and stubs for HardwareSerial stub
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;

// Unity setup/teardown
void setUp(void) {}
void tearDown(void) {}

using namespace bridge::test;

// Forward declaration of the test runner helper
void reset_bridge(BiStream& stream);
void sync_bridge(BiStream& stream);

void reset_bridge(BiStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "top-secret");
}

void sync_bridge(BiStream& stream) {
  stream.rx_buf.clear();
  stream.tx_buf.clear();
  auto ba = TestAccessor::create(Bridge);

  if (ba.isSharedSecretEmpty()) {
    const char* test_secret = "top-secret";
    etl::array<uint8_t, 32> secret_buf;
    secret_buf.fill(0);
    etl::copy_n(test_secret, strlen(test_secret), secret_buf.data());
    ba.setSharedSecret(etl::span<const uint8_t>(secret_buf.data(), 32));
  }

  rpc::payload::LinkSync sync_msg = {};
  etl::array<uint8_t, 16> nonce = {1, 2, 3, 4, 5, 6,  7,  8,
                                   9, 10, 11, 12, 13, 14, 15, 16};
  etl::copy_n(nonce.data(), 16, sync_msg.pb_msg.nonce.bytes);
  sync_msg.pb_msg.nonce.size = 16;

  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), 16, tag.data());
  etl::copy_n(tag.data(), 16, sync_msg.pb_msg.tag.bytes);
  sync_msg.pb_msg.tag.size = 16;

  rpc::Frame frame = {};
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> frame_buf;
  frame.payload = etl::span<uint8_t>(frame_buf.data(), frame_buf.size());
  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  frame.header.sequence_id = 1;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload =
      etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(frame, sync_msg);

  ba.onStartupStabilized();
  ba.dispatch(frame);

  int safety_counter = 0;
  while (safety_counter++ < 10 && !ba.isSynchronized()) {
    Bridge.process();
  }
}

void test_bridge_begin() {
  BiStream stream;
  reset_bridge(stream);
  auto ba = TestAccessor::create(Bridge);
  // New simplified FSM moves directly to UNSYNCHRONIZED on begin()
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_bridge_send_frame() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);

  const etl::array<uint8_t, 2> payload = {0xAA, 0xBB};
  TEST_ASSERT(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 123,
                               etl::span<const uint8_t>(payload.data(), 2)));
}

void test_bridge_process_rx() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);

  rpc::Frame frame = {};
  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_XOFF);
  frame.header.payload_length = 0;

  TestAccessor::create(Bridge).dispatch(frame);
}

void test_bridge_handshake() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  TEST_ASSERT(Bridge.isSynchronized());
}

void test_bridge_flow_control() {
  BiStream stream;
  reset_bridge(stream);
  auto ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  ba.setSynchronized();

  rpc::Frame xoff;
  xoff.header.version = rpc::PROTOCOL_VERSION;
  xoff.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_XOFF);
  xoff.header.payload_length = 0;
  ba.dispatch(xoff);
}

void test_bridge_dedup_console_write_retry() {
  BiStream stream;
  reset_bridge(stream);
  auto ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  ba.setSynchronized();
  Console.begin();

  rpc::payload::ConsoleWrite msg = {};
  etl::array<uint8_t, 5> data = {'h', 'e', 'l', 'l', 'o'};
  rpc::payload::copy_to_pb_bytes((pb_bytes_array_t*)&msg.pb_msg.data, 64,
                                 data.data(), 5);

  rpc::Frame frame = {};
  static etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> frame_buf;
  frame.payload = etl::span<uint8_t>(frame_buf.data(), frame_buf.size());
  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  frame.header.sequence_id = 10;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload =
      etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(frame, msg);

  etl::crc32 crc_calc;
  etl::array<uint8_t, rpc::FRAME_HEADER_SIZE> h;
  h[0] = 0x02;  // version
  etl::byte_stream_writer w(h.data() + 1, 6, etl::endian::big);
  w.write<uint16_t>(frame.header.payload_length);
  w.write<uint16_t>(frame.header.command_id);
  w.write<uint16_t>(frame.header.sequence_id);

  crc_calc.add(h.begin(), h.end());
  crc_calc.add(frame.payload.data(),
               frame.payload.data() + frame.header.payload_length);
  frame.crc = crc_calc.value();

  ba.dispatch(frame);
  Bridge.process();
  TEST_ASSERT_EQUAL(5, Console.available());
}

void test_bridge_ack_malformed_timeout_paths() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  etl::array<uint8_t, 1> payload = {'X'};
  (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 0,
                         etl::span<const uint8_t>(payload.data(), 1));
  Bridge.process();
}

void test_bridge_status_ack_uses_payload_command_id() {
  BiStream stream;
  reset_bridge(stream);
  auto ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  ba.setSynchronized();

  etl::array<uint8_t, 1> payload = {'X'};
  TEST_ASSERT_TRUE(Bridge.sendFrame(
      rpc::CommandId::CMD_CONSOLE_WRITE, 77,
      etl::span<const uint8_t>(payload.data(), payload.size())));
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  rpc::Frame malformed_ack = {};
  malformed_ack.header = {rpc::PROTOCOL_VERSION, 1,
                          rpc::to_underlying(rpc::StatusCode::STATUS_ACK), 77};
  static const etl::array<uint8_t, 1> malformed_payload = {0xC1};
  malformed_ack.nonce.fill(0);
  malformed_ack.tag.fill(0);
  malformed_ack.payload = etl::span<const uint8_t>(malformed_payload.data(),
                                                   malformed_payload.size());
  ba.dispatch(malformed_ack);
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> ack_payload;
  rpc::Frame ack = {};
  ack.header = {rpc::PROTOCOL_VERSION, 0,
                rpc::to_underlying(rpc::StatusCode::STATUS_ACK), 77};
  ack.nonce.fill(0);
  ack.tag.fill(0);
  ack.payload =
      etl::span<const uint8_t>(ack_payload.data(), ack_payload.size());
  bridge::test::set_pb_payload(
      ack, []() {
        rpc::payload::AckPacket p;
        p.pb_msg.command_id =
            rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
        return p;
      }());
  ba.dispatch(ack);

  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_status_ack_emits_original_command_id() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  stream.tx_buf.clear();

  TEST_ASSERT_TRUE(Bridge.send(
      rpc::StatusCode::STATUS_ACK, 42, []() {
        rpc::payload::AckPacket p;
        p.pb_msg.command_id =
            rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
        return p;
      }()));
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0);

  const size_t encoded_len =
      stream.tx_buf.data[stream.tx_buf.len - 1] == rpc::RPC_FRAME_DELIMITER
          ? stream.tx_buf.len - 1
          : stream.tx_buf.len;
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> decoded = {};
  const size_t decoded_len =
      TestCOBS::decode(stream.tx_buf.data.data(), encoded_len, decoded.data());
  TEST_ASSERT_TRUE(decoded_len >= rpc::MIN_FRAME_SIZE);

  rpc::FrameParser parser;
  auto parsed_frame =
      parser.parse(etl::span<const uint8_t>(decoded.data(), decoded_len));
  TEST_ASSERT_TRUE(parsed_frame.has_value());
  const rpc::Frame& ack = parsed_frame.value();
  TEST_ASSERT_EQUAL_UINT16(rpc::to_underlying(rpc::StatusCode::STATUS_ACK),
                           ack.header.command_id);
  TEST_ASSERT_EQUAL_UINT16(42, ack.header.sequence_id);

  auto parsed = rpc::Payload::parse<rpc::payload::AckPacket>(ack);
  TEST_ASSERT_TRUE(parsed.has_value());
  TEST_ASSERT_EQUAL_UINT16(rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE),
                           parsed.value().pb_msg.command_id);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_begin);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_flow_control);
  RUN_TEST(test_bridge_dedup_console_write_retry);
  RUN_TEST(test_bridge_ack_malformed_timeout_paths);
  RUN_TEST(test_bridge_status_ack_uses_payload_command_id);
  RUN_TEST(test_bridge_status_ack_emits_original_command_id);
  return UNITY_END();
}