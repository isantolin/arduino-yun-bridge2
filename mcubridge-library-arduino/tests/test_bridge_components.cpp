#include "TestUtils.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {

using namespace bridge::test;

static void reset_bridge_with_stream(RecordingStream& stream) {
  g_arduino_stream_delegate = &stream;
  Bridge.begin();
  Console.begin();
  TestAccessor::create(Bridge).setIdle();
}

static void restore_bridge_to_serial() {
  g_arduino_stream_delegate = nullptr;
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
  restore_bridge_to_serial();
}

static void test_datastore_put_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  DataStore.put("k", "v");
  Bridge.process();

  TEST_ASSERT(stream.tx_buffer.len > 0);
  restore_bridge_to_serial();
}

static void test_mailbox_send_outbound_frame() {
  RecordingStream stream;
  reset_bridge_with_stream(stream);
  Mailbox.send("msg");
  Bridge.process();

  TEST_ASSERT(stream.tx_buffer.len > 0);
  restore_bridge_to_serial();
}

}  // namespace

int main() {
  test_console_write_outbound_frame();
  test_datastore_put_outbound_frame();
  test_mailbox_send_outbound_frame();
  return 0;
}

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;
