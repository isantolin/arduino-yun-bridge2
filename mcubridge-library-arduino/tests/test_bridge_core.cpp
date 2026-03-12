#include "TestUtils.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {
bridge::test::RecordingStream g_null_stream;
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
HardwareSerial Serial;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using namespace bridge::test;

void reset_bridge(RecordingStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin(rpc::RPC_DEFAULT_BAUDRATE);
}

class TestFrameBuilder {
 public:
  static size_t build(uint8_t* out, size_t out_cap, uint16_t cmd_id,
                      const uint8_t* payload, size_t payload_len) {
    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
    size_t cursor = 0;
    raw[cursor++] = 0x02;
    raw[cursor++] = (payload_len >> 8) & 0xFF;
    raw[cursor++] = payload_len & 0xFF;
    raw[cursor++] = (cmd_id >> 8) & 0xFF;
    raw[cursor++] = cmd_id & 0xFF;
    if (payload_len > 0) {
      memcpy(&raw[cursor], payload, payload_len);
      cursor += payload_len;
    }
    etl::crc32 crc_calculator;
    crc_calculator.add(raw, raw + cursor);
    uint32_t crc = crc_calculator.value();
    raw[cursor++] = (crc >> 24) & 0xFF;
    raw[cursor++] = (crc >> 16) & 0xFF;
    raw[cursor++] = (crc >> 8) & 0xFF;
    raw[cursor++] = crc & 0xFF;
    size_t encoded_len = TestCOBS::encode(raw, cursor, out);
    out[encoded_len] = 0;
    return encoded_len + 1;
  }
};

void sync_bridge(RecordingStream& stream) {
  stream.tx_buffer.clear();
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
  uint8_t payload[32];
  memcpy(payload, nonce, 16);
  ba.computeHandshakeTag(nonce, 16, payload + 16);
  uint8_t encoded_frame[rpc::MAX_RAW_FRAME_SIZE + 32];
  const size_t frame_len =
      TestFrameBuilder::build(encoded_frame, sizeof(encoded_frame),
                              rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC),
                              payload, sizeof(payload));
  stream.inject_rx(encoded_frame, frame_len);
  Bridge.process();
  stream.tx_buffer.clear();
}

void test_bridge_begin() {
  RecordingStream stream;
  reset_bridge(stream);
  TEST_ASSERT(TestAccessor::create(Bridge).isUnsynchronized());
}

void test_bridge_send_frame() {
  RecordingStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  uint8_t payload[] = {0x01, 0x02, 0x03};
  bool result = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION,
                                 etl::span<const uint8_t>(payload, 3));
  TEST_ASSERT(result == true);
  TEST_ASSERT(stream.tx_buffer.len > 0);
}

void test_bridge_process_rx() {
  RecordingStream stream;
  reset_bridge(stream);
  uint8_t encoded_frame[128];
  const size_t encoded_len = TestFrameBuilder::build(
      encoded_frame, 128, rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION),
      nullptr, 0);
  stream.inject_rx(encoded_frame, encoded_len);
  Bridge.process();
}

void test_bridge_handshake() {
  RecordingStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  TEST_ASSERT(Bridge.isSynchronized());
}

void test_bridge_flow_control() {
  RecordingStream stream;
  reset_bridge(stream);
  rpc::Frame xoff;
  xoff.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_XOFF);
  xoff.header.payload_length = 0;
  TestAccessor::create(Bridge).dispatch(xoff);
}

void test_bridge_dedup_console_write_retry() {
  RecordingStream stream;
  reset_bridge(stream);
  rpc::Frame frame;
  uint8_t raw_frame[rpc::MAX_RAW_FRAME_SIZE];
  rpc::FrameBuilder builder;
  const uint8_t payload[] = {'h', 'e', 'l', 'l', 'o'};
  size_t raw_len =
      builder.build(etl::span<uint8_t>(raw_frame),
                    rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE),
                    etl::span<const uint8_t>(payload));
  frame.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);
  frame.header.payload_length = sizeof(payload);
  memcpy(frame.payload.data(), payload, sizeof(payload));
  frame.crc = rpc::read_u32_be(etl::span<const uint8_t>(&raw_frame[raw_len - rpc::CRC_TRAILER_SIZE], 4));
  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();
  g_test_millis = 0;
  ba.dispatch(frame);
  Bridge.process();
  TEST_ASSERT_EQ_UINT(Console.available(), sizeof(payload));
}

void test_bridge_ack_malformed_timeout_paths() {
  RecordingStream stream;
  reset_bridge(stream);
  sync_bridge(stream);
  const uint8_t payload[] = {'X'};
  Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE,
                   etl::span<const uint8_t>(payload, 1));
  g_test_millis += 5000;
  Bridge.process();
}

void test_bridge_chunking() {
  RecordingStream stream;
  reset_bridge(stream);
  TestAccessor::create(Bridge).setIdle();
  uint8_t header[5] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE};
  uint8_t data[100];
  for (size_t i = 0; i < 100; i++) data[i] = (uint8_t)i;
  Bridge.sendChunkyFrame(rpc::CommandId::CMD_MAILBOX_PROCESSED,
                         etl::span<const uint8_t>(header, 5),
                         etl::span<const uint8_t>(data, 100));
  TEST_ASSERT(stream.tx_buffer.len > 0);
}

}  // namespace

int main() {
  test_bridge_begin();
  test_bridge_send_frame();
  test_bridge_process_rx();
  test_bridge_handshake();
  test_bridge_flow_control();
  test_bridge_dedup_console_write_retry();
  test_bridge_ack_malformed_timeout_paths();
  test_bridge_chunking();
  return 0;
}
