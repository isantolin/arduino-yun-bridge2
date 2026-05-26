#include <unity.h>
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "BridgeTestHelper.h"

using namespace bridge::test;

void setUp() {}
void tearDown() {}

void test_component_dispatch() {
  Bridge.begin(115200, "6368616e67656d65313233");
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_PinMode msg = rpc_pb_PinMode_init_default;
  msg.pin = 13;
  msg.mode = 1; // OUTPUT

  rpc::Frame frame;
  set_pb_payload(frame, msg, rpc_pb_RpcPayload_set_pin_mode_tag);
  frame.envelope.sequence_id = 20;

  ba.dispatch(frame);
  Bridge.process();
}

void test_component_ack_processing() {
  Bridge.begin(115200, "6368616e67656d65313233");
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_AckPacket ack = rpc_pb_AckPacket_init_default;
  ack.command_id = rpc_pb_RpcPayload_digital_write_tag;

  rpc::Frame frame;
  set_pb_payload(frame, ack, rpc_pb_RpcPayload_ack_tag);
  frame.envelope.sequence_id = 30;

  ba.dispatch(frame);
  Bridge.process();
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_component_dispatch);
  RUN_TEST(test_component_ack_processing);
  return UNITY_END();
}
