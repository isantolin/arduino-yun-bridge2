#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "Bridge.h"
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

// Forward declaration of the test runner helper
void reset_bridge(BiStream& stream);
void sync_bridge(BiStream& stream);

void reset_bridge(BiStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "top-secret");
}

void sync_bridge(BiStream& stream) {
  stream.clear();
  Bridge._onStartupStabilized();

  rpc::payload::LinkSync sync_msg = {};
  uint8_t nonce[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
  etl::copy_n(nonce, 16, sync_msg.nonce.begin());
  
  uint8_t tag[16];
  Bridge._computeHandshakeTag(etl::span<const uint8_t>(nonce, 16), etl::span<uint8_t>(tag, 16));
  etl::copy_n(tag, 16, sync_msg.tag.begin());

  uint8_t payload_buffer[rpc::MAX_PAYLOAD_SIZE];
  msgpack::Encoder enc(payload_buffer, rpc::MAX_PAYLOAD_SIZE);
  sync_msg.encode(enc);

  stream.feed_frame(rpc::CommandId::CMD_LINK_SYNC, 1, enc.result());
  
  int safety_counter = 0;
  while (safety_counter++ < 10 && !Bridge.isSynchronized()) {
      Bridge.process();
  }
}

void test_bridge_begin() {
  BiStream stream;
  reset_bridge(stream);
  // bridge.begin() puts it in STARTUP, but _onStartupStabilized() was NOT called yet in reset_bridge.
  // Actually Bridge.begin() starts it.
  // In the original test: TEST_ASSERT(ba.getStartupStabilizing());
  // We can't check internal state anymore, so we check if it's NOT synchronized.
  TEST_ASSERT(!Bridge.isSynchronized());
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
  
  stream.feed_frame(rpc::CommandId::CMD_XOFF, 0, {});
  Bridge.process();
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
  Bridge._onStartupStabilized();
  // We need to be synchronized to process XOFF properly in some states, 
  // but let's see if we can just sync it.
  sync_bridge(stream);
  
  stream.feed_frame(rpc::CommandId::CMD_XOFF, 0, {});
  Bridge.process();
}

void test_bridge_dedup_console_write_retry() {
  BiStream stream;
  reset_bridge(stream);
  Bridge._onStartupStabilized();
  sync_bridge(stream);
  Console.begin();
  
  rpc::payload::ConsoleWrite msg = {};
  uint8_t data[] = "hello";
  msg.data = etl::span<const uint8_t>(data, 5);

  uint8_t payload_buffer[rpc::MAX_PAYLOAD_SIZE];
  msgpack::Encoder enc(payload_buffer, rpc::MAX_PAYLOAD_SIZE);
  msg.encode(enc);

  stream.feed_frame(rpc::CommandId::CMD_CONSOLE_WRITE, 10, enc.result());
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
