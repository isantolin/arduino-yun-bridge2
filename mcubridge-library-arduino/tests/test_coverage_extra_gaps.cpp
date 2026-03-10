/*
 * test_coverage_gap_filler.cpp - Additional tests to reach 100% Arduino coverage
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1

static unsigned long g_millis = 10000;
unsigned long millis() { return g_millis; }

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "test_support.h"

// Global instances
HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

namespace {

class TestStream : public Stream {
 public:
  ByteBuffer<8192> tx;
  ByteBuffer<8192> rx;
  size_t write(uint8_t c) override { tx.push(c); return 1; }
  size_t write(const uint8_t* b, size_t s) override { tx.append(b, s); return s; }
  int available() override { return rx.remaining(); }
  int read() override { return rx.read_byte(); }
  int peek() override { return rx.peek_byte(); }
  void flush() override {}
  void feed(const uint8_t* b, size_t s) { rx.append(b, s); }
};

void reset_env(TestStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin();
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setIdle();
  Console.begin();
  DataStore.reset();
}

// --- Bridge.cpp Gaps ---

void test_bridge_send_chunky_frame_overflow() {
  printf("  -> test_bridge_send_chunky_frame_overflow\n");
  TestStream stream;
  reset_env(stream);
  
  // Header + Data > MAX_PAYLOAD_SIZE should return false (implicit)
  uint8_t header[10];
  uint8_t data[1024]; 
  bool ok = Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE, 
                                   etl::span<const uint8_t>(header, 10),
                                   etl::span<const uint8_t>(data, 1024));
  TEST_ASSERT(!ok);
}

void test_bridge_is_security_check_passed_fail() {
  printf("  -> test_bridge_is_security_check_passed_fail\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  
  ba.setUnsynchronized();
  // CMD_DIGITAL_WRITE (81) requires sync
  TEST_ASSERT(!ba.isSecurityCheckPassed(81));
}

// --- DataStore.cpp Gaps ---

void test_datastore_value_truncation() {
  printf("  -> test_datastore_value_truncation\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  char long_val[300];
  memset(long_val, 'A', 299);
  long_val[299] = '\0';
  DataStore.put("key", long_val);
  
  // Trigger handle_get_request for this key
  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET);
  f.header.payload_length = 4;
  f.payload[0] = 3;
  memcpy(&f.payload[1], "key", 3);
  
  ba.dispatch(f);
}

void test_datastore_get_malformed() {
  printf("  -> test_datastore_get_malformed\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DATASTORE_GET);
  f.header.payload_length = 1;
  f.payload[0] = 10; // Key length 10 but payload only 1
  
  ba.dispatch(f);
}

// --- Console.cpp Gaps ---

void test_console_write_fail() {
  printf("  -> test_console_write_fail\n");
  TestStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  auto ca = bridge::test::ConsoleTestAccessor::create(Console);
  
  ba.setUnsynchronized();
  // Filling buffer manually to ensure it's full and won't be cleared
  while(!ca.isTxBufferFull()) {
    ca.pushTxByte('A');
  }
  
  // Next write should fail to flush and return 0
  size_t wrote = Console.write('B');
  TEST_ASSERT_EQ_UINT(wrote, 0);
}

} // namespace

int main() {
  printf("ARDUINO EXTRA GAPS TEST START\n");
  test_bridge_send_chunky_frame_overflow();
  test_bridge_is_security_check_passed_fail();
  test_datastore_value_truncation();
  test_datastore_get_malformed();
  test_console_write_fail();
  printf("ARDUINO EXTRA GAPS TEST END\n");
  return 0;
}

Stream* g_arduino_stream_delegate = nullptr;
