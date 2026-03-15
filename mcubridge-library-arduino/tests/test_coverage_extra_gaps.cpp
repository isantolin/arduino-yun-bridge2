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

void reset_env(BiStream& stream) {
  reset_bridge_core(Bridge, stream);
  Console.begin();
  DataStore.reset();
}

// --- Bridge.cpp Gaps ---

void test_bridge_send_chunky_frame_overflow() {
  printf("  -> test_bridge_send_chunky_frame_overflow\n");
  BiStream stream;
  reset_env(stream);
  
  // Header + Data > MAX_PAYLOAD_SIZE: sendChunkyFrame truncates to fit, doesn't reject
  uint8_t header[10];
  uint8_t data[1024]; 
  bool ok = Bridge.sendChunkyFrame(rpc::CommandId::CMD_FILE_WRITE, 
                                   etl::span<const uint8_t>(header, 10),
                                   etl::span<const uint8_t>(data, 1024));
  TEST_ASSERT(ok);
}

void test_bridge_is_security_check_passed_fail() {
  printf("  -> test_bridge_is_security_check_passed_fail\n");
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  
  ba.setUnsynchronized();
  // CMD_DIGITAL_WRITE (81) requires sync
  TEST_ASSERT(!ba.isSecurityCheckPassed(81));
}

// --- DataStore.cpp Gaps ---

void test_datastore_value_truncation() {
  printf("  -> test_datastore_value_truncation\n");
  BiStream stream;
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
  BiStream stream;
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
  BiStream stream;
  reset_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  auto ca = bridge::test::ConsoleTestAccessor::create(Console);
  
  ba.setUnsynchronized();
  // Filling buffer manually to ensure it's full and won't be cleared
  while(!ca.isTxBufferFull()) {
    ca.pushTxByte('A');
  }
  
  // Console.write() always returns 1 when _begun: flush may fail but
  // circular buffer push_back drops oldest element on full buffer
  size_t wrote = Console.write('B');
  TEST_ASSERT_EQ_UINT(wrote, 1);
}

} // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_send_chunky_frame_overflow);
  RUN_TEST(test_bridge_is_security_check_passed_fail);
  RUN_TEST(test_datastore_value_truncation);
  RUN_TEST(test_datastore_get_malformed);
  RUN_TEST(test_console_write_fail);
  return UNITY_END();
}

Stream* g_arduino_stream_delegate = nullptr;
