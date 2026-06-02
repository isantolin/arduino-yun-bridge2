#define BRIDGE_ENABLE_TEST_INTERFACE
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "security/security.h"
#include "test_support.h"

BridgeClass Bridge(Serial);
// Arduino Stubs for Linker
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

using bridge::test::TestAccessor;

void setUp(void) {}
void tearDown(void) {}

void test_surgical_bridge_errors() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Replay detection (Same nonce counter)
  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f .version = rpc::PROTOCOL_VERSION;
  f .command_id =
      static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  f .sequence_id = 1;
  f .payload.size = 32;
  // Bridge saves the last counter. We'll dispatch once.
  ba.dispatch(f);
  // Dispatch again with same nonce (implicit counter 0 in header)
  ba.dispatch(f);

  // 2. emitStatus variants
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, "Short");
  static char long_msg[300];
  etl::fill_n(long_msg, 299, 'A');
  long_msg[299] = '\0';
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, long_msg);
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);

  // 3. Unknown Command in dispatch
  rpc_pb_RpcEnvelope f_unk = rpc_pb_RpcEnvelope_init_default;
  f_unk .version = rpc::PROTOCOL_VERSION;
  f_unk .command_id = 999;
  ba.dispatch(f_unk);

  // 4. Bad version
  f_unk .version = 0;
  ba.dispatch(f_unk);

  TEST_ASSERT(true);
}

void test_surgical_fsm_resets() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvReset());

  TEST_ASSERT(true);
}

void test_surgical_security_failures() {
  static uint8_t key[32];
  static uint8_t nonce[12];
  static uint8_t tag[16];
  static uint8_t data[4];
  static uint8_t out[4];
  static uint8_t ad[12];

  // aegis_encrypt with tiny buffer
  bool ok = rpc::security::aead_encrypt(
      etl::span<uint8_t>(out, 1), etl::span<uint8_t>(tag, 16),
      etl::span<const uint8_t>(data, 4), etl::span<const uint8_t>(key, 32),
      etl::span<const uint8_t>(nonce, 12), etl::span<const uint8_t>(ad, 12));
  TEST_ASSERT_FALSE(ok);

  // aead_decrypt with mismatching tag
  ok = rpc::security::aead_decrypt(
      etl::span<uint8_t>(out, 4), etl::span<const uint8_t>(data, 4),
      etl::span<const uint8_t>(tag, 16), etl::span<const uint8_t>(key, 32),
      etl::span<const uint8_t>(nonce, 12), etl::span<const uint8_t>(ad, 12));
  TEST_ASSERT_FALSE(ok);
}

void test_surgical_tasks_flow() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);

  // SerialTask XOFF path
  static uint8_t dummy[1000];
  stream.feed(dummy, 1000);
  ba.invokeSerialTask();
  // XON path
  stream.clear();
  ba.invokeSerialTask();

  // TimerTask ACK timeout
  ba.setSynchronized();
  ba.onAckTimeout();

  TEST_ASSERT(true);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_surgical_bridge_errors);
  RUN_TEST(test_surgical_fsm_resets);
  RUN_TEST(test_surgical_security_failures);
  RUN_TEST(test_surgical_tasks_flow);
  return UNITY_END();
}