#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include <etl/span.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "security/security.h"
#include "test_support.h"

// Global for simulating time
static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }

namespace {
TxCaptureStream g_null_stream;
}  // namespace

// --- GLOBALS (required by Bridge.cpp when BRIDGE_TEST_NO_GLOBALS is 1) ---
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

namespace {
void setup_env(TxCaptureStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin();
  bridge::test::TestAccessor::create(Bridge).setIdle();
}

void test_fsm_gaps() {
  printf("  -> test_fsm_gaps\n");
  TxCaptureStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  ba.setUnsynchronized();
  assert(ba.isUnsynchronized());

  ba.setIdle();
  assert(ba.isIdle());
}

}  // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_fsm_gaps);
  return UNITY_END();
}

Stream* g_arduino_stream_delegate = nullptr;
