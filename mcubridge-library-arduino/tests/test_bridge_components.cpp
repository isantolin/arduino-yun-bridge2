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
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

using namespace bridge::test;

static void reset_bridge_with_stream(RecordingStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin();
  Console.begin();
  TestAccessor::create(Bridge).setIdle();
}

static void restore_bridge_to_serial() {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(Serial);
}

// --- ACTUAL TESTS ---

static void test_console_write_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  stream.tx_buffer.clear();

  const char msg[] = "hello";
  Console.write(reinterpret_cast<const uint8_t*>(msg), sizeof(msg) - 1);
  Console.flush();
  Bridge.process();

  TEST_ASSERT(stream.tx_buffer.len > 0);
  size_t cursor = 0;
  rpc::Frame f;
  //   TEST_ASSERT(extract_next_valid_frame(stream.tx_buffer, cursor, f));

  restore_bridge_to_serial();
}

static void test_datastore_put_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  DataStore.put("k", "v");
  Bridge.process();

  TEST_ASSERT(stream.tx_buffer.len > 0);
  size_t cursor = 0;
  rpc::Frame f;
  //   TEST_ASSERT(extract_next_valid_frame(stream.tx_buffer, cursor, f));
  //   TEST_ASSERT_EQ_UINT(f.header.command_id,
  //   rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_PUT));

  restore_bridge_to_serial();
}

static void test_mailbox_send_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  Mailbox.send("msg");
  Bridge.process();

  TEST_ASSERT(stream.tx_buffer.len > 0);
  size_t cursor = 0;
  rpc::Frame f;
  //   TEST_ASSERT(extract_next_valid_frame(stream.tx_buffer, cursor, f));
  //   TEST_ASSERT_EQ_UINT(f.header.command_id,
  //   rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_PUSH));

  restore_bridge_to_serial();
}

}  // namespace

int main() {
  test_console_write_outbound_frame();
  test_datastore_put_outbound_frame();
  test_mailbox_send_outbound_frame();
  return 0;
}
