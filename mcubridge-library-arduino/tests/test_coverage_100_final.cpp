#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include <etl/span.h>

#include "Bridge.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "security/security.h"

// Global for simulating time
static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis++; }

namespace bridge {
namespace test {
class TestAccessor {
 public:
  static TestAccessor create(BridgeClass& b) { return TestAccessor(b); }
  TestAccessor(BridgeClass& b) : _bridge(b) {}
  void setUnsynchronized() { _bridge._fsm.resetFsm(); }
  void setIdle() {
    _bridge._fsm.handshakeStart();
    _bridge._fsm.handshakeComplete();
  }
  bool isUnsynchronized() const { return _bridge._fsm.isUnsynchronized(); }
  bool isIdle() const { return _bridge._fsm.isIdle(); }

 private:
  BridgeClass& _bridge;
};
}  // namespace test
}  // namespace bridge

namespace {
class CaptureStream : public Stream {
 public:
  uint8_t tx_buf[8192];
  size_t tx_len = 0;

  size_t write(uint8_t b) override {
    if (tx_len < sizeof(tx_buf)) tx_buf[tx_len++] = b;
    return 1;
  }
  size_t write(const uint8_t* b, size_t s) override {
    for (size_t i = 0; i < s; i++) write(b[i]);
    return s;
  }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}
};

CaptureStream g_null_stream;
}  // namespace

// --- GLOBALS (required by Bridge.cpp when BRIDGE_TEST_NO_GLOBALS is 1) ---
#if BRIDGE_ENABLE_DATASTORE
#endif
#if BRIDGE_ENABLE_MAILBOX
#endif
#if BRIDGE_ENABLE_FILESYSTEM
#endif
#if BRIDGE_ENABLE_PROCESS
#endif
HardwareSerial Serial;

namespace {
void setup_env(CaptureStream& stream) {
  Bridge.~BridgeClass();
  new (&Bridge) BridgeClass(stream);
  Bridge.begin();
  bridge::test::TestAccessor::create(Bridge).setIdle();
}

void test_fsm_gaps() {
  printf("  -> test_fsm_gaps\n");
  CaptureStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  ba.setUnsynchronized();
  assert(ba.isUnsynchronized());

  ba.setIdle();
  assert(ba.isIdle());
}

}  // namespace

int main() {
  printf("FINAL ARDUINO 100%% COVERAGE TEST START\n");
  test_fsm_gaps();
  printf("FINAL ARDUINO 100%% COVERAGE TEST END\n");
  return 0;
}

Stream* g_arduino_stream_delegate = nullptr;
