#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
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

void test_fsm_timeout_fault() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);

  localBridge._onStartupStabilized();
  simulate_handshake(localBridge, stream);

  TEST_ASSERT(localBridge.isSynchronized());

  // Simulate timeout forcing by advancing time and processing
  for (int i = 0; i < 20; i++) {
    g_test_millis += 10000;
    localBridge.process();
  }

  TEST_ASSERT(!localBridge.isSynchronized());
}

void test_crc_error_escalation() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);

  localBridge._onStartupStabilized();
  // We feed corrupted frame (wrong version)
  uint8_t corrupt[] = {0xFF, 0x00, 0x01};
  stream.feed(corrupt, 3);
  localBridge.process();
}

void test_ack_timeout_retry_exceeded() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);

  simulate_handshake(localBridge, stream);

  uint8_t payload[] = {0x00};
  (void)localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 1,
                              etl::span<const uint8_t>(payload, 1));

  // Trigger timeout until unsynced
  for (int i = 0; i < 30; i++) {
    g_test_millis += 10000;
    localBridge.process();
  }

  TEST_ASSERT(!localBridge.isSynchronized());
}

void test_error_policy_direct() {
}

void test_service_edge_cases_exhaustive() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  simulate_handshake(localBridge, stream);

  // 1. Console write null
  Console.write(nullptr, 0);

  // 2. FileSystem read response malformed
  static uint8_t buf[256];
  msgpack::Encoder enc(buf, sizeof(buf));
  enc.write_array(0);  // Wrong array size for FileReadResponse (expects 1)

  stream.feed_frame(rpc::CommandId::CMD_FILE_READ_RESP, 10, enc.result());
  localBridge.process();

  // 3. SPIService end when not started
  SPIService.end();
}

void test_spi_real_logic_exhaustive() {
  BiStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200);
  simulate_handshake(localBridge, stream);

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
