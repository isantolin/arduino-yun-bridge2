#include "Bridge.h"
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
  localBridge._onStartupStabilized();

  rpc::payload::LinkSync sync_msg = {};
  uint8_t nonce[16] = {0};
  etl::copy_n(nonce, 16, sync_msg.nonce.begin());
  
  uint8_t tag[16];
  localBridge._computeHandshakeTag(etl::span<const uint8_t>(nonce, 16), etl::span<uint8_t>(tag, 16));
  etl::copy_n(tag, 16, sync_msg.tag.begin());

  uint8_t payload_buffer[rpc::MAX_PAYLOAD_SIZE];
  msgpack::Encoder enc(payload_buffer, rpc::MAX_PAYLOAD_SIZE);
  sync_msg.encode(enc);

  stream.feed_frame(rpc::CommandId::CMD_LINK_SYNC, 1, enc.result());
  
  int safety = 0;
  while (safety++ < 10 && !localBridge.isSynchronized()) {
    localBridge.process();
  }
}

void integrated_test_components() {
  BiStream stream;
  reset_bridge_core(Bridge, stream, 115200, "top-secret");
  
  // Real handshake
  rpc::payload::LinkSync sync_msg = {};
  uint8_t nonce[16] = {0};
  uint8_t tag[16];
  Bridge._computeHandshakeTag(etl::span<const uint8_t>(nonce, 16), etl::span<uint8_t>(tag, 16));
  etl::copy_n(tag, 16, sync_msg.tag.begin());
  uint8_t payload_buffer[rpc::MAX_PAYLOAD_SIZE];
  msgpack::Encoder enc(payload_buffer, rpc::MAX_PAYLOAD_SIZE);
  sync_msg.encode(enc);
  stream.feed_frame(rpc::CommandId::CMD_LINK_SYNC, 1, enc.result());
  
  int safety = 0;
  while (safety++ < 10 && !Bridge.isSynchronized()) {
    Bridge.process();
  }

  Console.begin();
  Console.write('H');
  Console.process();
  TEST_ASSERT(stream.tx_buf.len > 0);

  FileSystem.remove("test.txt");
  
#if BRIDGE_ENABLE_DATASTORE
  uint8_t val[] = {1};
  DataStore.set("k", etl::span<const uint8_t>(val, 1));
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
