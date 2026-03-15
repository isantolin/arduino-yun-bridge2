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

// Global Instances
BridgeClass Bridge(g_test_stream);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

void test_fsm_initial_state() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200);
  TEST_ASSERT(localBridge.isUnsynchronized());
  printf("  -> Initial state: OK\n");
}

void test_mutual_auth_success() {
  BridgeClass localBridge(g_test_stream);
  const char* secret = "secret_1234567890123456";
  localBridge.begin(115200, secret, 23);
  auto accessor = bridge::test::TestAccessor::create(localBridge);

  // Prepare valid SYNC frame with correct Tag
  uint8_t nonce[16] = {0xAA};
  uint8_t tag[16];

  // Internal helper to compute expected tag
  accessor.computeHandshakeTag(nonce, 16, tag);

  rpc::Frame sync_frame;
  sync_frame.header.version = rpc::PROTOCOL_VERSION;
  sync_frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  rpc::payload::LinkSync sync_msg = mcubridge_LinkSync_init_default;
  sync_msg.nonce.size = 16;
  memcpy(sync_msg.nonce.bytes, nonce, 16);
  sync_msg.tag.size = 16;
  memcpy(sync_msg.tag.bytes, tag, 16);
  bridge::test::set_pb_payload(sync_frame, sync_msg);

  accessor.dispatch(sync_frame);

  TEST_ASSERT(localBridge.isSynchronized());
  TEST_ASSERT(localBridge.isIdle());
  printf("  -> Mutual Auth Success: OK\n");
}

void test_mutual_auth_failure_wrong_tag() {
  BridgeClass localBridge(g_test_stream);
  const char* secret = "secret_1234567890123456";
  localBridge.begin(115200, secret, 23);
  auto accessor = bridge::test::TestAccessor::create(localBridge);

  uint8_t nonce[16] = {0xAA};
  uint8_t wrong_tag[16] = {0xFF};

  rpc::Frame sync_frame;
  sync_frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  rpc::payload::LinkSync sync_msg = mcubridge_LinkSync_init_default;
  sync_msg.nonce.size = 16;
  memcpy(sync_msg.nonce.bytes, nonce, 16);
  sync_msg.tag.size = 16;
  memcpy(sync_msg.tag.bytes, wrong_tag, 16);
  bridge::test::set_pb_payload(sync_frame, sync_msg);

  accessor.dispatch(sync_frame);

  TEST_ASSERT(!localBridge.isSynchronized());
  TEST_ASSERT(localBridge.isFault());
  printf("  -> Mutual Auth Failure (Wrong Tag): OK\n");
}

void test_mutual_auth_failure_malformed_length() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200, "secret", 6);
  auto accessor = bridge::test::TestAccessor::create(localBridge);

  rpc::Frame sync_frame;
  sync_frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  // Corrupt payload with invalid data that pb_decode will reject
  sync_frame.payload[0] = 0xFF; // Invalid protobuf tag
  sync_frame.header.payload_length = 1;

  accessor.dispatch(sync_frame);

  TEST_ASSERT(localBridge.isUnsynchronized());
  printf("  -> Mutual Auth Failure (Malformed Length): OK\n");
}

void test_fsm_transitions_running() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200);
  auto accessor = bridge::test::TestAccessor::create(localBridge);

  // Sync without secret
  rpc::Frame sync_frame;
  sync_frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  rpc::payload::LinkSync sync_msg = mcubridge_LinkSync_init_default;
  sync_msg.nonce.size = 16;
  bridge::test::set_pb_payload(sync_frame, sync_msg);
  accessor.dispatch(sync_frame);
  TEST_ASSERT(localBridge.isSynchronized());

  // Send a command that requires ACK
  localBridge.sendFrame(rpc::CommandId::CMD_SET_PIN_MODE);
  TEST_ASSERT(localBridge.isAwaitingAck());

  // Receive ACK
  rpc::Frame ack_frame;
  ack_frame.header.command_id = rpc::to_underlying(rpc::StatusCode::STATUS_ACK);
  rpc::payload::AckPacket ack_msg = mcubridge_AckPacket_init_default;
  ack_msg.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  bridge::test::set_pb_payload(ack_frame, ack_msg);
  accessor.dispatch(ack_frame);

  TEST_ASSERT(localBridge.isIdle());
  printf("  -> FSM Transitions (Idle -> AwaitingAck -> Idle): OK\n");
}

void test_fsm_timeout_to_unsynchronized() {
  BridgeClass localBridge(g_test_stream);
  localBridge.begin(115200);
  auto accessor = bridge::test::TestAccessor::create(localBridge);

  // Sync
  rpc::Frame sync_frame;
  sync_frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  rpc::payload::LinkSync sync_msg = mcubridge_LinkSync_init_default;
  sync_msg.nonce.size = 16;
  bridge::test::set_pb_payload(sync_frame, sync_msg);
  accessor.dispatch(sync_frame);

  // Disable retries for immediate timeout
  accessor.setAckRetryLimit(0);

  // Send command, wait for ACK
  localBridge.sendFrame(rpc::CommandId::CMD_SET_PIN_MODE);
  TEST_ASSERT(localBridge.isAwaitingAck());

  // Explicitly trigger ACK timeout via accessor
  accessor.onAckTimeout();

  TEST_ASSERT(localBridge.isUnsynchronized());
  printf("  -> FSM Timeout to Unsynchronized: OK\n");
}

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_initial_state);
  RUN_TEST(test_mutual_auth_success);
  RUN_TEST(test_mutual_auth_failure_wrong_tag);
  RUN_TEST(test_mutual_auth_failure_malformed_length);
  RUN_TEST(test_fsm_transitions_running);
  RUN_TEST(test_fsm_timeout_to_unsynchronized);
  return UNITY_END();
}
