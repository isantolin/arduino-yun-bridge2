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

void test_surgical_bridge_errors() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  rpc_pb_Empty empty = rpc_pb_Empty_init_default;

  // Unknown command tag
  bridge::test::set_pb_payload(f, empty, 999);
  ba.dispatch(f);

  // malformed tag
  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_malformed_tag);
  ba.dispatch(f);
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_surgical_bridge_errors);
  return UNITY_END();
}
