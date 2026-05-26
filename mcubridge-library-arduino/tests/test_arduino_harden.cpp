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

void test_bridge_protocol_version_mismatch() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  f.envelope.version = 99; // wrong version
  rpc_pb_Empty empty = rpc_pb_Empty_init_default;
  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_get_version_tag);
  ba.dispatch(f);
}

void test_bridge_unknown_command_tag() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  rpc_pb_Empty empty = rpc_pb_Empty_init_default;
  bridge::test::set_pb_payload(f, empty, 254); // unknown tag
  ba.dispatch(f);
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_protocol_version_mismatch);
  RUN_TEST(test_bridge_unknown_command_tag);
  return UNITY_END();
}
