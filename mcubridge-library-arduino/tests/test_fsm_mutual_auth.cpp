#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "security/security.h"
#include "services/SPIService.h"
#include "test_support.h"

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }
void delay(unsigned long ms) { g_test_millis += ms; }

using namespace rpc;
using namespace bridge;

BiStream g_test_stream;
Stream* g_arduino_stream_delegate = &g_test_stream;
HardwareSerial Serial;
HardwareSerial Serial1;

void test_fsm_initial_state() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200);
  localBridge._onStartupStabilized();
  TEST_ASSERT(!localBridge.isSynchronized());
}

void test_mutual_auth_success() {
  BridgeClass localBridge(g_test_stream);
  const char* secret = "secret_1234567890123456";
  localBridge.begin(115200, secret);
  
  simulate_handshake(localBridge, g_test_stream);
  TEST_ASSERT(localBridge.isSynchronized());
}

void test_mutual_auth_failure_wrong_tag() {
  BridgeClass localBridge(g_test_stream);
  const char* secret = "secret_1234567890123456";
  localBridge.begin(115200, secret);
  localBridge._onStartupStabilized();
  
  rpc::payload::LinkSync sync_msg = {};
  etl::fill(sync_msg.nonce.begin(), sync_msg.nonce.end(), 0x11);
  etl::fill(sync_msg.tag.begin(), sync_msg.tag.end(), 0xEE); // Wrong tag
  
  uint8_t payload_buffer[rpc::MAX_PAYLOAD_SIZE];
  msgpack::Encoder enc(payload_buffer, rpc::MAX_PAYLOAD_SIZE);
  sync_msg.encode(enc);

  g_test_stream.feed_frame(rpc::CommandId::CMD_LINK_SYNC, 1, enc.result());
  localBridge.process();
  
  TEST_ASSERT(!localBridge.isSynchronized());
}

void test_fsm_timeout_to_unsynchronized() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200);
  simulate_handshake(localBridge, g_test_stream);
  
  uint8_t payload[] = {0x01};
  TEST_ASSERT_TRUE(localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 0, etl::span<const uint8_t>(payload, 1)));
  
  // Force timeouts by advancing time and processing
  for (int i = 0; i < 20; i++) {
    g_test_millis += 10000; // Advance 10s
    localBridge.process();
  }
  
  TEST_ASSERT(!localBridge.isSynchronized());
}

void setUp(void) {
    g_test_stream.clear();
}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_initial_state);
  RUN_TEST(test_mutual_auth_success);
  RUN_TEST(test_mutual_auth_failure_wrong_tag);
  RUN_TEST(test_fsm_timeout_to_unsynchronized);
  return UNITY_END();
}
