#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

// --- GLOBALS ---
unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

namespace {
BiStream g_null_stream;
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

using bridge::test::TestAccessor;

void test_fsm_gaps() {
  printf("test_fsm_gaps: Creating TestAccessor\n");
  auto ba = TestAccessor::create(Bridge);
  printf("test_fsm_gaps: Calling fsmResetFsm\n");
  ba.fsmResetFsm();
  printf("test_fsm_gaps: Checking isUnsynchronized\n");
  TEST_ASSERT(ba.isUnsynchronized());
  printf("test_fsm_gaps: Done\n");
}

void test_structs_gaps() {
  // Nanopb coverage
}

} // namespace

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  printf("Test started\n");
  Bridge.begin(115200);
  UNITY_BEGIN();
  printf("Running test_fsm_gaps\n");
  RUN_TEST(test_fsm_gaps);
  printf("Running test_structs_gaps\n");
  RUN_TEST(test_structs_gaps);
  return UNITY_END();
}
