#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <unity.h>
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "test_support.h"

void setUp() {}
void tearDown() {}

namespace {
using bridge::test::TestAccessor;

void test_bridge_hardened_basic() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  rpc_pb_Empty empty = rpc_pb_Empty_init_default;
  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_get_version_tag);
  ba.dispatch(f);
}
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_hardened_basic);
  return UNITY_END();
}
