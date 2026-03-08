#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include <etl/span.h>

#include "Bridge.h"
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

HardwareSerial Serial;
HardwareSerial Serial1;

// Bridge, Console, DataStore, etc. are defined in the library.
// We just need to make sure they use our mock Serial if they are constructed with it.
// BRIDGE_DEFAULT_SERIAL_PORT defaults to Serial.

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
  Bridge.begin(115200, "secret");
  auto accessor = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame sync;
  sync.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  sync.header.payload_length = 32;  // 16 nonce + 16 tag
  uint8_t nonce[16];
  etl::fill_n(nonce, 16, uint8_t{0xAA});
  memcpy(sync.payload.data(), nonce, 16);

  uint8_t tag[16];
  accessor.computeHandshakeTag(nonce, 16, tag);
  memcpy(sync.payload.data() + 16, tag, 16);

  accessor.dispatch(sync);
  TEST_ASSERT(Bridge.isSynchronized());

  rpc::Frame gpio;
  gpio.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_SET_PIN_MODE);
  gpio.header.payload_length = 2;
  gpio.payload[0] = 13;
  gpio.payload[1] = 1;
  accessor.dispatch(gpio);

  Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE,
                        etl::span<const uint8_t>((const uint8_t*)"X", 1));
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

int main() {
  printf("INTEGRATED ARDUINO TEST START\n");
  fflush(stdout);
  Bridge.begin(115200);
  bridge::test::TestAccessor::create(Bridge).setIdle();

  printf("Running: integrated_test_rle\n");
  integrated_test_rle();
  printf("Running: integrated_test_protocol\n");
  integrated_test_protocol();
  printf("Running: integrated_test_bridge_core\n");
  integrated_test_bridge_core();
  printf("Running: integrated_test_components\n");
  integrated_test_components();
  printf("Running: integrated_test_error_branches\n");
  integrated_test_error_branches();
  printf("Running: integrated_test_extreme_coverage\n");
  integrated_test_extreme_coverage();

  printf("INTEGRATED ARDUINO TEST END\n");
  fflush(stdout);
  return 0;
}

Stream* g_arduino_stream_delegate = nullptr;
