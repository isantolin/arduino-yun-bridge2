#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <etl/array.h>
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "test_support.h"
#include "services/Console.h"
#include "services/FileSystem.h"
#include "services/Process.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"

// Bridge and core services are already provided by production code.
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

void integrated_test_bridge_core() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200, "test_secret_1234567890123456");
  auto& accessor = bridge::test::TestAccessor::create(localBridge);
  accessor.onStartupStabilized();

  rpc::Frame sync;
  sync.header.version = rpc::PROTOCOL_VERSION;
  sync.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  sync.header.payload_length = 16;
  sync.header.sequence_id = 1;
  etl::array<uint8_t, 16> payload = {0};
  sync.payload = etl::span<const uint8_t>(payload.data(), 16);
  
  accessor.dispatch(sync);
}

void integrated_test_components() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  Console.begin();
  Console.write('H');
  Console.process();
  TEST_ASSERT(stream.tx_buf.len > 0);

  FileSystem.remove("test.txt");
  
#if BRIDGE_ENABLE_DATASTORE
  etl::array<uint8_t, 1> val = {1};
  DataStore.set("k", etl::span<const uint8_t>(val.data(), 1));
#endif

#if BRIDGE_ENABLE_MAILBOX
  Mailbox.requestRead();
#endif

  Process.kill(123);
}

} // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(integrated_test_bridge_core);
  RUN_TEST(integrated_test_components);
  return UNITY_END();
}
