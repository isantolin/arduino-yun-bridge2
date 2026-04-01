#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"
#include "services/SPIService.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {
BiStream g_null_stream;
}

BridgeClass Bridge(g_null_stream);
ConsoleClass Console;
#if BRIDGE_ENABLE_DATASTORE
DataStoreClass DataStore;
#endif
#if BRIDGE_ENABLE_MAILBOX
MailboxClass Mailbox;
#endif
#if BRIDGE_ENABLE_FILESYSTEM
FileSystemClass FileSystem;
#endif
#if BRIDGE_ENABLE_PROCESS
ProcessClass Process;
#endif
#if BRIDGE_ENABLE_SPI
SPIServiceClass SPIService;
#endif
HardwareSerial Serial;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using bridge::test::TestAccessor;

void reset_bridge(BiStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
}

class TestFrameBuilder {
 public:
  static size_t build(uint8_t* out, size_t out_cap, uint16_t cmd_id,
                      const uint8_t* payload, size_t payload_len, uint16_t seq_id = 0) {
    // [SIL-2] Delegate to production FrameBuilder for header+CRC serialization
    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
    const size_t raw_len = rpc::FrameBuilder::build(
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
    ba.assignSharedSecret(
        reinterpret_cast<const uint8_t*>(test_secret),
        reinterpret_cast<const uint8_t*>(test_secret) + strlen(test_secret));
  }
  ba.setStartupStabilizing(false);
  const uint8_t nonce[16] = {1, 2,  3,  4,  5,  6,  7,  8,
                             9, 10, 11, 12, 13, 14, 15, 16};

  rpc::payload::LinkSync sync_msg = {};
  memcpy(sync_msg.nonce, nonce, 16);
  ba.computeHandshakeTag(nonce, 16, sync_msg.tag);

  rpc::Frame frame = {};
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(frame, sync_msg);

  uint8_t encoded_frame[rpc::MAX_RAW_FRAME_SIZE + 32];
  const size_t encoded_len = TestFrameBuilder::build(
      encoded_frame, sizeof(encoded_frame),
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), payload_buffer.data(),
      frame.header.payload_length);
  stream.feed(encoded_frame, encoded_len);

  // Process until all bytes are drained from the mock stream
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
  etl::span<const uint8_t> span(data, 5);
  rpc::util::pb_setup_encode_span(msg.data, span);

  frame.header.version = rpc::PROTOCOL_VERSION;
  frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  frame.payload = etl::span<const uint8_t>(payload_buffer.data(), payload_buffer.size());
  bridge::test::set_pb_payload(frame, msg);

  // Calculate CRC for deduplication using etl::crc32
  uint8_t header_buf[rpc::FRAME_HEADER_SIZE];
  header_buf[0] = frame.header.version;
  rpc::write_u16_be(etl::span<uint8_t>(header_buf + 1, 2),
                    frame.header.payload_length);
  rpc::write_u16_be(etl::span<uint8_t>(header_buf + 3, 2),
                    frame.header.command_id);

  etl::crc32 crc_calc;
  crc_calc.add(header_buf, header_buf + rpc::FRAME_HEADER_SIZE);
  crc_calc.add(frame.payload.data(),
               frame.payload.data() + frame.header.payload_length);
  frame.crc = crc_calc.value();

  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();
  Console.begin();
  g_test_millis = 0;
  ba.dispatch(frame);
  Bridge.process();
  TEST_ASSERT_EQ_UINT(Console.available(), 5);
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
