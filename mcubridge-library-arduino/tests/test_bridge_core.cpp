#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "Bridge.h"
#include "protocol/rpc_frame.h"
#include "services/Console.h"
#include "test_constants.h"
#include "test_support.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"

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
  auto& ba = TestAccessor::create(Bridge);
  
  if (ba.isSharedSecretEmpty()) {
    const char* test_secret = "top-secret";
    etl::array<uint8_t, 32> secret_buf;
    secret_buf.fill(0);
    memcpy(secret_buf.data(), test_secret, strlen(test_secret));
    ba.setSharedSecret(etl::span<const uint8_t>(secret_buf.data(), 32));
  }

  rpc::payload::LinkSync sync_msg = {};
  uint8_t nonce[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
  etl::copy_n(nonce, 16, sync_msg.nonce.begin());
  
  uint8_t tag[16];
  ba.computeHandshakeTag(nonce, 16, tag);
  etl::copy_n(tag, 16, sync_msg.tag.begin());

  rpc::Frame frame = {};
  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  frame.header.sequence_id = 1;
  
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
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
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT(ba.getStartupStabilizing());
}

void test_bridge_send_frame() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  
  const uint8_t payload[] = {0xAA, 0xBB};
  TEST_ASSERT(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 123, etl::span<const uint8_t>(payload, 2)));
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
  auto& ba = TestAccessor::create(Bridge);
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
  auto& ba = TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  ba.setSynchronized();
  Console.begin();
  
  rpc::payload::ConsoleWrite msg = {};
  uint8_t data[] = "hello";
  msg.data = etl::span<const uint8_t>(data, 5);

  rpc::Frame frame = {};
  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  frame.header.sequence_id = 10;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(frame, msg);

  etl::crc32 crc_calc;
  uint8_t h[rpc::FRAME_HEADER_SIZE];
  h[0] = frame.header.version;
  rpc::write_u16_be(etl::span<uint8_t>(h + 1, 2), frame.header.payload_length);
  rpc::write_u16_be(etl::span<uint8_t>(h + 3, 2), frame.header.command_id);
  rpc::write_u16_be(etl::span<uint8_t>(h + 5, 2), frame.header.sequence_id);
  
  crc_calc.add(h, h + rpc::FRAME_HEADER_SIZE);
  crc_calc.add(frame.payload.data(), frame.payload.data() + frame.header.payload_length);
  frame.crc = crc_calc.value();

  ba.dispatch(frame);
  Bridge.process();
  TEST_ASSERT_EQUAL(5, Console.available());
}

void test_bridge_ack_malformed_timeout_paths() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  const uint8_t payload[] = {'X'};
  (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 0, etl::span<const uint8_t>(payload, 1));
  Bridge.process();
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
  return UNITY_END();
}
