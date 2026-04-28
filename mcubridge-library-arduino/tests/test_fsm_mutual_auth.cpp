#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
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

TxCaptureStream g_test_stream;
Stream* g_arduino_stream_delegate = &g_test_stream;
HardwareSerial Serial;
HardwareSerial Serial1;

void test_fsm_initial_state() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200);
  auto& accessor = bridge::test::TestAccessor::create(localBridge);
  accessor.onStartupStabilized();
  TEST_ASSERT(accessor.isUnsynchronized());
}

void test_mutual_auth_success() {
  BridgeClass localBridge(g_test_stream);
  const char* secret = "secret_1234567890123456";
  localBridge.begin(115200, secret);
  auto& accessor = bridge::test::TestAccessor::create(localBridge);
  accessor.onStartupStabilized();
  
  const uint8_t nonce[16] = {1, 2,  3,  4,  5,  6,  7,  8, 9, 10, 11, 12, 13, 14, 15, 16};
  rpc::payload::LinkSync sync_msg = {};
  memcpy(sync_msg.nonce.data(), nonce, 16);
  accessor.computeHandshakeTag(nonce, 16, sync_msg.tag.data());
  
  rpc::Frame sync_frame = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  sync_frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(sync_frame, sync_msg);
  sync_frame.header.version = rpc::PROTOCOL_VERSION;
  sync_frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  sync_frame.header.sequence_id = 1;
  sync_frame.header.payload_length = 48; // Approximation
  
  accessor.dispatch(sync_frame);
  TEST_ASSERT(accessor.isSynchronized());
}

void test_mutual_auth_failure_wrong_tag() {
  BridgeClass localBridge(g_test_stream);
  const char* secret = "secret_1234567890123456";
  localBridge.begin(115200, secret);
  auto& accessor = bridge::test::TestAccessor::create(localBridge);
  accessor.onStartupStabilized();
  
  const uint8_t nonce[16] = {1, 2, 3, 4};
  rpc::payload::LinkSync sync_msg = {};
  memcpy(sync_msg.nonce.data(), nonce, 16);
  memset(sync_msg.tag.data(), 'X', 16); // Invalid tag
  
  rpc::Frame sync_frame = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  sync_frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(sync_frame, sync_msg);
  sync_frame.header.version = rpc::PROTOCOL_VERSION;
  sync_frame.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  sync_frame.header.sequence_id = 1;
  sync_frame.header.payload_length = 48;
  
  accessor.dispatch(sync_frame);
  TEST_ASSERT(accessor.getStartupStabilizing());
}

void test_fsm_timeout_to_unsynchronized() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200);
  auto& accessor = bridge::test::TestAccessor::create(localBridge);
  accessor.onStartupStabilized();
  accessor.setSynchronized();
  
  uint8_t payload[] = {0x01};
  TEST_ASSERT_TRUE(localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 0, etl::span<const uint8_t>(payload, 1)));
  TEST_ASSERT(accessor.isAwaitingAck());
  
  g_test_millis += 50000;
  for (int i = 0; i < 15; i++) {
    accessor.onAckTimeout();
  }
  
  TEST_ASSERT(accessor.isFault() || accessor.isUnsynchronized());
}

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_initial_state);
  RUN_TEST(test_mutual_auth_success);
  RUN_TEST(test_mutual_auth_failure_wrong_tag);
  RUN_TEST(test_fsm_timeout_to_unsynchronized);
  return UNITY_END();
}
