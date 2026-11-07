#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include <etl/array.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "test_support.h"

// Bridge and core services are already provided by production code.
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

static BiStream g_test_stream;

void integrated_test_bridge_core() {
  g_test_stream.clear();
  reset_bridge_core(Bridge, g_test_stream, 0, "top-secret");
  auto& accessor = bridge::test::TestAccessor::create(Bridge);

  rpc_pb_LinkSync sync_req = rpc_pb_LinkSync_init_default;
  memset(sync_req.nonce.bytes, 1, 16);
  sync_req.nonce.size = 16;
  accessor.computeHandshakeTag(sync_req.nonce.bytes, 16, sync_req.tag.bytes);
  sync_req.tag.size = 16;

  uint8_t payload_buf[64];
  pb_ostream_t ostream =
      pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
  TEST_ASSERT_TRUE(pb_encode(&ostream, rpc_pb_LinkSync_fields, &sync_req));

  etl::array<uint8_t, 256> frame_buf;
  size_t frame_len = build_hdlc_rpc_frame(
      1, pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.SystemService"),
      pw::tokenizer::Hash("LinkSync"),
      etl::span<const uint8_t>(payload_buf, ostream.bytes_written), frame_buf,
      1, 1);
  TEST_ASSERT_TRUE(frame_len > 0);

  accessor.feedHdlcBytes(etl::span<const uint8_t>(frame_buf.data(), frame_len));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void integrated_test_components() {
  g_test_stream.clear();
  reset_bridge_core(Bridge, g_test_stream, 0, "");
  auto& ba = bridge::test::TestAccessor::create(Bridge);
  ba.setSynchronized();

  Console.begin();
  Console.write('H');
  Console.process();
  TEST_ASSERT(g_test_stream.tx_buf.len > 0);

  FileSystem.remove("test.txt");

#if BRIDGE_ENABLE_DATASTORE
  etl::array<uint8_t, 1> val = {1};
  DataStore.set("k", etl::span<const uint8_t>(val.data(), 1));
#endif

#if BRIDGE_ENABLE_MAILBOX
  TEST_ASSERT_TRUE(Mailbox.requestRead());
#endif

  Process.kill(123);
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(integrated_test_bridge_core);
  RUN_TEST(integrated_test_components);
  return UNITY_END();
}
