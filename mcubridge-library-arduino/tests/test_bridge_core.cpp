#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

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
  sync_msg.nonce.size = 16;
  memcpy(sync_msg.nonce.bytes, nonce, 16);
  sync_msg.tag.size = 16;
  ba.computeHandshakeTag(nonce, 16, sync_msg.tag.bytes);

  rpc::Frame frame;
  bridge::test::set_pb_payload(frame, sync_msg);

  uint8_t encoded_frame[rpc::MAX_RAW_FRAME_SIZE + 32];
  const size_t frame_len = TestFrameBuilder::build(
      encoded_frame, sizeof(encoded_frame),
      rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), frame.payload.data(),
      frame.header.payload_length);
  stream.feed(encoded_frame, frame_len);
  Bridge.process();
  stream.tx_buf.clear();
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
  bool result = Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION,
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
  Bridge.process();
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
  Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE,
                   etl::span<const uint8_t>(payload, 1));
  g_test_millis += 5000;
  Bridge.process();
}

void test_bridge_chunking() {
  BiStream stream;
  reset_bridge(stream);
  auto ba = TestAccessor::create(Bridge); // Using the standard global Bridge
  ba.setIdle();
  uint8_t header[5] = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE};
  uint8_t data[10] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
  
  Bridge.sendChunkyFrame(rpc::CommandId::CMD_MAILBOX_PROCESSED,
                         etl::span<const uint8_t>(header, 5),
                         etl::span<const uint8_t>(data, 10));
                         
  TEST_ASSERT(stream.tx_buf.len > 0);
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
  RUN_TEST(test_bridge_chunking);
  return UNITY_END();
}
