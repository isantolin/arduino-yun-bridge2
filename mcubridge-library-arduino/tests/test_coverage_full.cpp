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

void test_bridge_coverage() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::Frame f = {};
  rpc_pb_Empty empty = rpc_pb_Empty_init_default;

  // Unknown command via tag
  bridge::test::set_pb_payload(f, empty, 999);
  ba.dispatch(f);

  // Version
  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_get_version_tag);
  ba.dispatch(f);

  // Free Memory
  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_get_free_memory_tag);
  ba.dispatch(f);

  // Pin read (Digital)
  rpc_pb_PinRead pr = rpc_pb_PinRead_init_default;
  pr.pin = 13;
  bridge::test::set_pb_payload(f, pr, rpc_pb_RpcPayload_digital_read_tag);
  ba.dispatch(f);

  // Digital Write
  rpc_pb_DigitalWrite dw = rpc_pb_DigitalWrite_init_default;
  dw.pin = 13;
  dw.value = 1;
  bridge::test::set_pb_payload(f, dw, rpc_pb_RpcPayload_digital_write_tag);
  ba.dispatch(f);

  // SPI Config
  rpc_pb_SpiConfig sc = rpc_pb_SpiConfig_init_default;
  sc.frequency = 1000000;
  bridge::test::set_pb_payload(f, sc, rpc_pb_RpcPayload_spi_config_tag);
  ba.dispatch(f);

  // File Write
  rpc_pb_FileWrite fw = rpc_pb_FileWrite_init_default;
  strncpy(fw.path, "test.txt", 64);
  uint8_t data[] = "test";
  rpc::payload::copy_to_pb_bytes(fw.data, data, 4);
  bridge::test::set_pb_payload(f, fw, rpc_pb_RpcPayload_file_write_tag);
  ba.dispatch(f);

  // STATUS OK/MALFORMED
  bridge::test::set_pb_payload(f, empty, rpc_pb_RpcPayload_ok_tag);
  ba.dispatch(f);
}

} // namespace

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_coverage);
  return UNITY_END();
}
