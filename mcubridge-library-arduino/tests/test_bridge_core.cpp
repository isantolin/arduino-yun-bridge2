#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"
#include "services/SPIService.h"
#include "services/Console.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {
BiStream g_null_stream;
}

// Bridge and core services are already provided by production code.
HardwareSerial Serial;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using bridge::test::TestAccessor;

void reset_bridge(BiStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE, "test_secret_1234567890123456");
}

class TestFrameBuilder {
 public:
  static size_t build(uint8_t* out, size_t out_cap, uint16_t cmd_id,
                      const uint8_t* payload, size_t payload_len, uint16_t seq_id = 0) {
    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
    rpc::FrameBuilder builder;
    const size_t raw_len = builder.build(
        etl::span<uint8_t>(raw, sizeof(raw)), cmd_id, seq_id,
        etl::span<const uint8_t>(payload, payload_len));
    if (raw_len == 0 || raw_len + 2 > out_cap) return 0;

    out[0] = 0; // Prepend delimiter
    size_t encoded_len = TestCOBS::encode(raw, raw_len, out + 1);
    out[encoded_len + 1] = 0; // Append delimiter
    return encoded_len + 2;
  }
};

void sync_bridge(BiStream& stream) {
  stream.tx_buf.clear();
  auto ba = TestAccessor::create(Bridge);
  if (ba.isSharedSecretEmpty()) {
    const char* test_secret = "test_secret";
    etl::array<uint8_t, 32> secret_buf;
    secret_buf.fill(0);
    memcpy(secret_buf.data(), test_secret, strlen(test_secret));
    ba.setSharedSecret(etl::span<const uint8_t>(secret_buf.data(), 32));
  }
  ba.onStartupStabilized();
  const uint8_t nonce[16] = {1, 2,  3,  4,  5,  6,  7,  8,
                             9, 10, 11, 12, 13, 14, 15, 16};

  rpc::payload::LinkSync sync_msg = {};
  memcpy(sync_msg.nonce.data(), nonce, 16);
  ba.computeHandshakeTag(nonce, 16, sync_msg.tag.data());

  rpc::Frame frame = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(frame, sync_msg);

  uint8_t encoded_frame[rpc::MAX_RAW_FRAME_SIZE + 32];
  const size_t encoded_len = TestFrameBuilder::build(
      encoded_frame, sizeof(encoded_frame),
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), payload_buffer.data(),
      frame.header.payload_length, 1);
  stream.feed(encoded_frame, encoded_len);

  int safety_counter = 0;
  while (stream.available() > 0 && safety_counter++ < 100) {
      Bridge.process();
  }
}

void test_bridge_begin() {
  BiStream stream;
  reset_bridge(stream);
  auto ba = TestAccessor::create(Bridge);
  TEST_ASSERT(ba.getStartupStabilizing());
  ba.onStartupStabilized();
  TEST_ASSERT(ba.isUnsynchronized());
}

void test_bridge_send_frame() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  uint8_t payload[] = {0x01, 0x02, 0x03};
  bool result = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, 0,
                                 etl::span<const uint8_t>(payload, 3));
  TEST_ASSERT(result == true);
  TEST_ASSERT(stream.tx_buf.len > 0);
}

void test_bridge_process_rx() {
  BiStream stream;
  reset_bridge(stream);
  uint8_t encoded_frame[128];
  const size_t encoded_len = TestFrameBuilder::build(
      encoded_frame, 128, rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION),
      nullptr, 0);
  stream.feed(encoded_frame, encoded_len);
  
  int safety_counter = 0;
  while (stream.available() > 0 && safety_counter++ < 10) {
      Bridge.process();
  }
}

void test_bridge_handshake() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  TEST_ASSERT(Bridge.isSynchronized());
}

void test_bridge_flow_control() {
  BiStream stream;
  reset_bridge(stream);
  rpc::Frame xoff;
  xoff.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_XOFF);
  xoff.header.payload_length = 0;
  TestAccessor::create(Bridge).dispatch(xoff);
}

void test_bridge_dedup_console_write_retry() {
  BiStream stream;
  reset_bridge(stream);
  rpc::Frame frame;

  rpc::payload::ConsoleWrite msg = {};
  uint8_t data[] = "hello";
  msg.data = etl::span<const uint8_t>(data, 5);

  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  frame.header.sequence_id = 10;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(frame, msg);

  etl::crc32 crc_calc;
  uint8_t h[rpc::FRAME_HEADER_SIZE];
  h[0] = frame.header.version;
  rpc::write_u16_be(etl::span<uint8_t>(h + 1, 2), frame.header.payload_length);
  rpc::write_u16_be(etl::span<uint8_t>(h + 3, 2), frame.header.command_id);
  rpc::write_u16_be(etl::span<uint8_t>(h + 5, 2), frame.header.sequence_id);
  
  crc_calc.add(h, h + rpc::FRAME_HEADER_SIZE);
  crc_calc.add(frame.payload.data(), frame.payload.data() + frame.header.payload_length);
  frame.crc = crc_calc.value();

  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();
  Console.begin();
  g_test_millis = 0;
  ba.dispatch(frame);
  Bridge.process();
  TEST_ASSERT_EQUAL(5, Console.available());
}

void test_bridge_ack_malformed_timeout_paths() {
  BiStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  const uint8_t payload[] = {'X'};
  (void)Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 0,
                   etl::span<const uint8_t>(payload, 1));
  g_test_millis += 5000;
  Bridge.process();
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_begin);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_flow_control);
  RUN_TEST(test_bridge_dedup_console_write_retry);
  RUN_TEST(test_bridge_ack_malformed_timeout_paths);
  return UNITY_END();
}
