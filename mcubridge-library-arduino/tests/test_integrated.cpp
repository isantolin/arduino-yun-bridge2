#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define BRIDGE_TEST_NO_GLOBALS 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include <etl/span.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rle.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "security/security.h"
#include "services/SPIService.h"
#include "test_support.h"

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }

using namespace rpc;
using namespace bridge;

// --- MOCKS ---

BiStream g_bridge_stream;
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(g_bridge_stream);
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

Stream* g_arduino_stream_delegate = &g_bridge_stream;

// --- TEST SUITES ---

void integrated_test_rle() {
  uint8_t in[] = "AAAAABBBCCCC";
  uint8_t enc[32], dec[32];
  size_t el = rle::encode(etl::span<const uint8_t>(in, 12),
                          etl::span<uint8_t>(enc, 32));
  size_t dl = rle::decode(etl::span<const uint8_t>(enc, el),
                          etl::span<uint8_t>(dec, 32));
  TEST_ASSERT(dl == 12 && memcmp(in, dec, 12) == 0);
}

void integrated_test_protocol() {
  FrameBuilder b;
  FrameParser p;
  uint8_t raw[128];
  uint8_t pl[] = {0x01, 0x02, 0x03};
  size_t rl = b.build(etl::span<uint8_t>(raw, 128), 0x100,
                      etl::span<const uint8_t>(pl, 3));
  auto result = p.parse(etl::span<const uint8_t>(raw, rl));
  TEST_ASSERT(result.has_value());
  Frame f = result.value();
  TEST_ASSERT(f.header.command_id == 0x100);
}

void integrated_test_bridge_core() {
  TxCaptureStream stream;
  BridgeClass localBridge(stream);
  localBridge.begin(115200, "secret");
  auto accessor = bridge::test::TestAccessor::create(localBridge);
  accessor.onStartupStabilized();

  rpc::Frame sync;
  sync.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  
  uint8_t nonce[16];
  etl::fill_n(nonce, 16, uint8_t{0xAA});
  uint8_t tag[16];
  accessor.computeHandshakeTag(nonce, 16, tag);
  
  rpc::payload::LinkSync sync_msg = {};
  memcpy(sync_msg.nonce, nonce, 16);
  memcpy(sync_msg.tag, tag, 16);

  // Create handshake payload using mutable sync object payload
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload_buffer;
  pb_ostream_t out_stream = pb_ostream_from_buffer(payload_buffer.data(), payload_buffer.size());
  pb_encode(&out_stream, rpc::Payload::Descriptor<rpc::payload::LinkSync>::fields(), &sync_msg);
  sync.header.payload_length = static_cast<uint16_t>(out_stream.bytes_written);
  sync.payload = etl::span<const uint8_t>(payload_buffer.data(), sync.header.payload_length);

  accessor.dispatch(sync);
  TEST_ASSERT(localBridge.isSynchronized());
}

void integrated_test_components() {
  Console.begin();
  Console.write((uint8_t)'t');
  Console.flush();

#if BRIDGE_ENABLE_DATASTORE
  DataStore.set("k", etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>("v"), 1));
#endif
#if BRIDGE_ENABLE_MAILBOX
  Mailbox.write(etl::span<const uint8_t>(reinterpret_cast<const uint8_t*>("m"), 1));
#endif
}

void integrated_test_error_branches() {
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, F("err"));
  Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
  Bridge.enterSafeState();
}

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  Bridge.begin(115200);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.onStartupStabilized();
  ba.setIdle();
  UNITY_BEGIN();
  RUN_TEST(integrated_test_rle);
  RUN_TEST(integrated_test_protocol);
  RUN_TEST(integrated_test_bridge_core);
  RUN_TEST(integrated_test_components);
  RUN_TEST(integrated_test_error_branches);
  return UNITY_END();
}
