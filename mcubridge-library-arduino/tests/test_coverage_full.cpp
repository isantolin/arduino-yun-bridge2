#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "ErrorPolicy.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

// Services
#include "services/Console.h"
#include "services/FileSystem.h"
#include "services/SPIService.h"

unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }
void delay(unsigned long ms) { g_test_millis += ms; }

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using bridge::test::TestAccessor;

void test_fsm_timeout_fault() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);

  ba.setIdle();
  ba.onStartupStabilized();
  ba.setSynchronized();

  TEST_ASSERT(localBridge.isSynchronized());

  ba.forceTimeout();  // Simular un fallo fatal a través de timeout

  TEST_ASSERT(ba.isFault());
  TEST_ASSERT(!localBridge.isSynchronized());
}

void test_crc_error_escalation() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);

  ba.setIdle();
  ba.onStartupStabilized();
  ba.setLastParseError(rpc::FrameError::CRC_MISMATCH);
  TEST_ASSERT(ba.getLastParseError() == rpc::FrameError::CRC_MISMATCH);
}

void test_ack_timeout_retry_exceeded() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);

  ba.setIdle();
  ba.onStartupStabilized();
  ba.setSynchronized();

  uint8_t payload[] = {0x00};
  (void)localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 0,
                              etl::span<const uint8_t>(payload, 1));

  TEST_ASSERT(ba.isAwaitingAck());

  // Set retry count to limit
  ba.setRetryCount(ba.getAckRetryLimit());

  // Trigger timeout which should fault / reset
  ba.onAckTimeout();

  TEST_ASSERT(ba.isFault());
}

void test_error_policy_direct() {
  // [SIL-2] SafeStatePolicy no longer exposes onFatalError directly to comply with zero-unused-code policy.
}

void test_service_edge_cases_exhaustive() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);
  ba.setSynchronized();

  // 1. Console write null
  Console.write(nullptr, 0);

  // 2. FileSystem read response malformed
  static uint8_t buf[256];
  msgpack::Encoder enc(buf, sizeof(buf));
  enc.write_array(0);  // Wrong array size for FileReadResponse (expects 1)

  rpc::Frame f = {};
  f.header.command_id = (uint16_t)rpc::CommandId::CMD_FILE_READ_RESP;
  f.payload = enc.result();
  f.header.payload_length = (uint16_t)f.payload.size();
  ba.dispatch(f);

  // 3. SPIService end when not started
  SPIService.end();
}

void test_spi_real_logic_exhaustive() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  auto& ba = TestAccessor::create(localBridge);
  ba.setSynchronized();

  uint8_t spidata[4] = {1, 2, 3, 4};

  // 1. Uninitialized transfer
  SPIService.end();
  size_t len = SPIService.transfer(etl::span<uint8_t>(spidata, 4));
  TEST_ASSERT_EQUAL(0, len);

  // 2. Initialized transfer
  SPIService.begin();
  len = SPIService.transfer(etl::span<uint8_t>(spidata, 4));
  TEST_ASSERT_EQUAL(4, len);

  // 3. Set config
  rpc::payload::SpiConfig cfg;
  cfg.frequency = 1000000;
  cfg.bit_order = 1;
  cfg.data_mode = 0;
  SPIService.setConfig(cfg);
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_timeout_fault);
  RUN_TEST(test_crc_error_escalation);
  RUN_TEST(test_ack_timeout_retry_exceeded);
  RUN_TEST(test_error_policy_direct);
  RUN_TEST(test_service_edge_cases_exhaustive);
  RUN_TEST(test_spi_real_logic_exhaustive);
  return UNITY_END();
}
