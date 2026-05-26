#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <unity.h>
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "test_support.h"

void setUp() {}
void tearDown() {}

namespace {
using bridge::test::TestAccessor;

void test_bridge_full_crypto_handshake_and_data() {
  BiStream stream;
  reset_bridge_core(Bridge, stream, 0, "6368616e67656d65313233");
  auto ba = TestAccessor::create(Bridge);

  // 1. Handshake
  etl::array<uint8_t, 16> nonce; nonce.fill(0x42);
  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), nonce.size(), tag.data());

  rpc_pb_LinkSync sync = rpc_pb_LinkSync_init_default;
  etl::copy_n(nonce.begin(), 16, sync.nonce.bytes);
  sync.nonce.size = 16;
  etl::copy_n(tag.begin(), 16, sync.tag.bytes);
  sync.tag.size = 16;

  rpc::Frame f = {};
  bridge::test::set_pb_payload(f, sync, rpc_pb_RpcPayload_link_sync_tag);
  ba.dispatch(f);

  TEST_ASSERT(Bridge.isSynchronized());
}
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_full_crypto_handshake_and_data);
  return UNITY_END();
}
