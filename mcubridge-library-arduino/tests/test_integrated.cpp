#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include <etl/span.h>

#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rle.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "security/security.h"
#include "test_support.h"

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }

using namespace rpc;
using namespace bridge;

// --- MOCKS ---

BiStream g_bridge_stream;
HardwareSerial Serial;
HardwareSerial Serial1;
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
BridgeClass Bridge(g_bridge_stream);

// --- TEST SUITES ---

void integrated_test_rle() {
  uint8_t in[] = "AAAAABBBCCCC";
  uint8_t enc[32], dec[32];
  size_t el = rle::encode(etl::span<const uint8_t>(in, 12),
                          etl::span<uint8_t>(enc, 32));
  size_t dl = rle::decode(etl::span<const uint8_t>(enc, el),
                          etl::span<uint8_t>(dec, 32));
  TEST_ASSERT(dl == 12 && memcmp(in, dec, 12) == 0);

  uint8_t in2[] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  el = rle::encode(etl::span<const uint8_t>(in2, 5),
                   etl::span<uint8_t>(enc, 32));
  dl = rle::decode(etl::span<const uint8_t>(enc, el),
                   etl::span<uint8_t>(dec, 32));
  TEST_ASSERT(dl == 5 && memcmp(in2, dec, 5) == 0);
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

  rpc::Frame sync;
  sync.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  
  uint8_t nonce[16];
  etl::fill_n(nonce, 16, uint8_t{0xAA});
  uint8_t tag[16];
  accessor.computeHandshakeTag(nonce, 16, tag);

  rpc::payload::LinkSync sync_msg = {};
  sync_msg.nonce.size = 16;
  memcpy(sync_msg.nonce.bytes, nonce, 16);
  sync_msg.tag.size = 16;
  memcpy(sync_msg.tag.bytes, tag, 16);

  pb_ostream_t out_stream = pb_ostream_from_buffer(sync.payload.data(), sync.payload.size());
  pb_encode(&out_stream, rpc::Payload::Descriptor<rpc::payload::LinkSync>::fields(), &sync_msg);
  sync.header.payload_length = static_cast<uint16_t>(out_stream.bytes_written);

  accessor.dispatch(sync);
  TEST_ASSERT(localBridge.isSynchronized());

  rpc::Frame gpio;
  gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  
  rpc::payload::PinMode gpio_msg = {13, 1};
  out_stream = pb_ostream_from_buffer(gpio.payload.data(), gpio.payload.size());
  pb_encode(&out_stream, rpc::Payload::Descriptor<rpc::payload::PinMode>::fields(), &gpio_msg);
  gpio.header.payload_length = static_cast<uint16_t>(out_stream.bytes_written);
  
  accessor.dispatch(gpio);

  rpc::payload::ConsoleWrite console_msg = {};
  console_msg.data.size = 1;
  console_msg.data.bytes[0] = 'X';
  
  uint8_t console_buf[64];
  out_stream = pb_ostream_from_buffer(console_buf, sizeof(console_buf));
  pb_encode(&out_stream, rpc::Payload::Descriptor<rpc::payload::ConsoleWrite>::fields(), &console_msg);

  localBridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE,
                        etl::span<const uint8_t>(console_buf, out_stream.bytes_written));
  accessor.retransmitLastFrame();
}

void integrated_test_components() {
  Console.begin();
  Console.write((uint8_t)'t');
  Console.flush();

#if BRIDGE_ENABLE_DATASTORE
  DataStore.put("k", "v");
#endif
#if BRIDGE_ENABLE_MAILBOX
  Mailbox.send("m");
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  FileSystem.read("f");
#endif
#if BRIDGE_ENABLE_PROCESS
  Process.runAsync("ls");
#endif
}

void integrated_test_error_branches() {
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, "err");
  Bridge.emitStatus(rpc::StatusCode::STATUS_OVERFLOW);
  Bridge.enterSafeState();
  TEST_ASSERT(rpc::security::run_cryptographic_self_tests());
}

void integrated_test_extreme_coverage() {
  auto accessor = bridge::test::TestAccessor::create(Bridge);

// ... (rest of function omitted for brevity) ...

// 20. Callbacks
#if BRIDGE_ENABLE_DATASTORE
  DataStore.onDataStoreGetResponse(
      DataStoreClass::DataStoreGetHandler::create([](etl::string_view k, etl::span<const uint8_t> v) {
        (void)k;
        (void)v;
      }));
#endif
#if BRIDGE_ENABLE_FILESYSTEM
  FileSystem.onFileSystemReadResponse(
      FileSystemClass::FileSystemReadHandler::create([](etl::span<const uint8_t> d) {
        (void)d;
      }));
#endif
#if BRIDGE_ENABLE_PROCESS
  Process.onProcessRunAsyncResponse(
      ProcessClass::ProcessRunAsyncHandler::create([](int16_t pid) {
        (void)pid;
      }));
  Process.onProcessPollResponse(
      ProcessClass::ProcessPollHandler::create([](rpc::StatusCode s, uint8_t ec,
                                                  etl::span<const uint8_t> out,
                                                  etl::span<const uint8_t> err) {
        (void)s;
        (void)ec;
        (void)out;
        (void)err;
      }));
  Process.onProcessRunAsyncResponse(
      ProcessClass::ProcessRunAsyncHandler::create([](int16_t p) { (void)p; }));
#endif
#if BRIDGE_ENABLE_MAILBOX
  Mailbox.onMailboxMessage(
      MailboxClass::MailboxHandler::create([](etl::span<const uint8_t> m) {
        (void)m;
      }));
  Mailbox.onMailboxAvailableResponse(
      MailboxClass::MailboxAvailableHandler::create([](uint16_t c) { (void)c; }));
#endif
}

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  Bridge.begin(115200);
  bridge::test::TestAccessor::create(Bridge).setIdle();
  UNITY_BEGIN();
  RUN_TEST(integrated_test_rle);
  RUN_TEST(integrated_test_protocol);
  RUN_TEST(integrated_test_bridge_core);
  RUN_TEST(integrated_test_components);
  RUN_TEST(integrated_test_error_branches);
  RUN_TEST(integrated_test_extreme_coverage);
  return UNITY_END();
}

Stream* g_arduino_stream_delegate = nullptr;
