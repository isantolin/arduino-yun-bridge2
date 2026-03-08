#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1
static unsigned long g_test_millis = 10000;  // Start at non-zero
unsigned long millis() { return g_test_millis++; }

#include "Bridge.h"

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "BridgeTestInterface.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_constants.h"
#include "test_support.h"

// --- GLOBALS ---
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace {

class CaptureStream : public Stream {
 public:
  ByteBuffer<4096> tx;
  ByteBuffer<4096> rx;
  size_t write(uint8_t c) override {
    tx.push(c);
    return 1;
  }
  size_t write(const uint8_t* b, size_t s) override {
    tx.append(b, s);
    return s;
  }
  int available() override { return rx.remaining(); }
  int read() override { return rx.read_byte(); }
  int peek() override { return -1; }
  void flush() override {}
  void feed(const uint8_t* b, size_t s) { rx.append(b, s); }
};

void setup_env(CaptureStream& stream) {
  g_arduino_stream_delegate = &stream;
  Bridge.begin(115200);
  auto ba = bridge::test::TestAccessor::create(Bridge);
  ba.setIdle();
}

// --- ACTUAL TESTS ---
void test_bridge_gaps() {
  CaptureStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  rpc::Frame f;
  memset(&f, 0, sizeof(f));

  // Gap: dispatch unexpected status codes
  f.header.command_id = 0x3F;  // STATUS_CODE_MAX
  ba.dispatch(f);

  // Gap: dispatch with compressed flag but decode failure (short payload)
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE) |
                        rpc::RPC_CMD_FLAG_COMPRESSED;
  f.header.payload_length = 1;
  f.payload[0] = 0xFF;  // RLE escape sin datos
  ba.dispatch(f);

  // Gap: _isRecentDuplicateRx branches
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  f.header.payload_length = 2;
  f.payload[0] = 13;
  f.payload[1] = 1;
  f.crc = 0x12345678;  // Dummy CRC
  ba.setAckTimeoutMs(1000);
  ba.setAckRetryLimit(3);
  ba.markRxProcessed(f);
  g_test_millis += 1500;  // Move into the retry window (elapsed > ack_timeout)
  assert(ba.isRecentDuplicateRx(f));

  // Gap: enterSafeState reset logic
  Bridge.enterSafeState();
  assert(!Bridge.isSynchronized());

  // Gap: LinkSync without secret
  ba.clearSharedSecret();
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC);
  f.header.payload_length = rpc::RPC_HANDSHAKE_NONCE_LENGTH;
  memset(f.payload.data(), 0xAA, rpc::RPC_HANDSHAKE_NONCE_LENGTH);
  ba.dispatch(f);

  // Gap: onPacketReceived with various errors
  uint8_t crc_err[] = {0x02, 0x00, 0x00, 0x40, 0x00, 0xDE, 0xAD, 0xBE, 0xEF};
  stream.feed(crc_err, sizeof(crc_err));
  Bridge.process();

  // Gap: Retransmission logic and failure streak
  ba.setUnsynchronized();
  ba.fsmHandshakeStart();
  ba.fsmHandshakeComplete();
  ba.fsmSendCritical();
  ba.setAckTimeoutMs(1000);
  ba.setAckRetryLimit(1);
  ba.setRetryCount(ba.getAckRetryLimit());

  Bridge.onAckTimeout();
  assert(!Bridge.isSynchronized());
}

void test_datastore_gaps() {
#if BRIDGE_ENABLE_DATASTORE
  CaptureStream stream;
  setup_env(stream);

  // Gap: _trackPendingDatastoreKey overflow
  for (int i = 0; i < BRIDGE_MAX_PENDING_DATASTORE + 1; ++i) {
    DataStore.requestGet("key");
  }
#endif
}

void test_console_gaps() {
  CaptureStream stream;
  setup_env(stream);
  Console.begin();
  auto ca = bridge::test::ConsoleTestAccessor::create(Console);

  // Gap: write(buffer, size) chunking
  uint8_t large_buf[rpc::MAX_PAYLOAD_SIZE + 10];
  memset(large_buf, 'A', sizeof(large_buf));
  Console.write(large_buf, sizeof(large_buf));

  // Gap: read() high/low watermarks
  for (int i = 0; i < BRIDGE_CONSOLE_RX_BUFFER_SIZE; ++i)
    ca.pushRxByte(static_cast<uint8_t>(i));
  ca.setXoffSent(true);
  while (!ca.isRxBufferEmpty()) Console.read();
  assert(!ca.getXoffSent());

  // Gap: flush() with empty buffer
  ca.clearTxBuffer();
  Console.flush();
}

void test_filesystem_gaps() {
#if BRIDGE_ENABLE_FILESYSTEM
  CaptureStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  // Gap: write with data too large
  uint8_t super_large[rpc::MAX_PAYLOAD_SIZE + 10];
  FileSystem.write("test.txt", etl::span<const uint8_t>(super_large, sizeof(super_large)));

  // Gap: read() with invalid path
  FileSystem.read("");
  char long_path[rpc::RPC_MAX_FILEPATH_LENGTH + 5];
  memset(long_path, 'p', sizeof(long_path));
  long_path[sizeof(long_path) - 1] = '\0';
  FileSystem.read(long_path);

  // Gap: remove with overflowed path
  FileSystem.remove(long_path);

  // Gap: handleResponse with valid read handler
  FileSystem.onFileSystemReadResponse(
      FileSystemClass::FileSystemReadHandler::create([](etl::span<const uint8_t> d) {
        (void)d;
      }));
  rpc::Frame f;
  f.header.command_id = rpc::to_underlying(rpc::CommandId::CMD_FILE_READ_RESP);
  f.header.payload_length = 4;
  memcpy(f.payload.data(), "\0\x02OK", 4);
  ba.dispatch(f);
#endif
}

void test_mailbox_gaps() {
#if BRIDGE_ENABLE_MAILBOX
  CaptureStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  Mailbox.requestRead();
  Mailbox.requestAvailable();

  rpc::Frame f;
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  f.header.payload_length = 2;
  rpc::write_u16_be(f.payload.data(), 5);
  ba.dispatch(f);
#endif
}

void test_process_gaps() {
#if BRIDGE_ENABLE_PROCESS
  CaptureStream stream;
  setup_env(stream);
  auto ba = bridge::test::TestAccessor::create(Bridge);

  Process.runAsync("test");
  rpc::Frame f_pid;
  f_pid.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  f_pid.header.payload_length = 2;
  rpc::write_u16_be(f_pid.payload.data(), 42);
  ba.dispatch(f_pid);
  Process.poll(42);
  Process.kill(42);

  rpc::Frame f;
  f.header.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  f.header.payload_length = 8;
  f.payload[0] = 0x30;  // OK
  f.payload[1] = 0;     // exit_code
  rpc::write_u16_be(&f.payload[2], 1);
  f.payload[4] = 'o';
  rpc::write_u16_be(&f.payload[5], 1);
  f.payload[7] = 'e';
  ba.dispatch(f);
#endif
}

}  // namespace

int main() {
  printf("EXTREME ARDUINO COVERAGE V2 START\n");
  test_bridge_gaps();
  test_datastore_gaps();
  test_console_gaps();
  test_filesystem_gaps();
  test_mailbox_gaps();
  test_process_gaps();
  printf("EXTREME ARDUINO COVERAGE V2 END\n");
  return 0;
}
