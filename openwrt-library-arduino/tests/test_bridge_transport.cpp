#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

// Must be defined before including Bridge.h to expose private fields on host.
#define BRIDGE_HOST_TEST 1

#include "Bridge.h"
#include "arduino/BridgeTransport.h"
#include "protocol/cobs.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "test_constants.h"
#include "test_support.h"

using namespace rpc;

// Define global Serial instances for the Arduino stub.
HardwareSerial Serial;
HardwareSerial Serial1;

// Define the globals that are normally provided by Bridge.cpp when
// BRIDGE_TEST_NO_GLOBALS is not set.
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

namespace {

enum class WriteMode {
  Normal,
  ShortAlways,
  TerminatorFailsOnSecondCall,
};

class VectorStream : public Stream {
 public:
  ByteBuffer<8192> tx;
  ByteBuffer<8192> rx;

  WriteMode mode = WriteMode::Normal;
  int buffer_write_calls = 0;

  size_t write(uint8_t c) override {
    TEST_ASSERT(tx.push(c));
    return 1;
  }

  size_t write(const uint8_t* buffer, size_t size) override {
    buffer_write_calls++;

    if (!buffer || size == 0) {
      return 0;
    }

    if (mode == WriteMode::ShortAlways) {
      const size_t n = (size > 0) ? (size - 1) : 0;
      TEST_ASSERT(tx.append(buffer, n));
      return n;
    }

    if (mode == WriteMode::TerminatorFailsOnSecondCall && buffer_write_calls >= 2) {
      // Simulate a missing terminator write.
      return 0;
    }

    TEST_ASSERT(tx.append(buffer, size));
    return size;
  }

  int available() override {
    return static_cast<int>(rx.remaining());
  }

  int read() override {
    return rx.read_byte();
  }

  int peek() override {
    return rx.peek_byte();
  }

  void flush() override {}

  void inject_rx(const uint8_t* data, size_t len) {
    TEST_ASSERT(rx.append(data, len));
  }

  void clear_tx() {
    tx.clear();
    buffer_write_calls = 0;
  }
};

static void test_cobs_null_guards() {
  uint8_t dst[8] = {0};
  uint8_t src[2] = {1, 2};

  TEST_ASSERT(cobs::encode(nullptr, 1, dst) == 0);
  TEST_ASSERT(cobs::encode(src, sizeof(src), nullptr) == 0);

  TEST_ASSERT(cobs::decode(nullptr, 1, dst) == 0);
  TEST_ASSERT(cobs::decode(src, sizeof(src), nullptr) == 0);
}

static void test_transport_sendFrame_rejects_oversized_payload() {
  VectorStream stream;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(rpc::RPC_DEFAULT_BAUDRATE);

  uint8_t payload[rpc::MAX_PAYLOAD_SIZE + 1];
  test_memfill(payload, sizeof(payload), rpc::RPC_TEST_PAYLOAD_BYTE);
  TEST_ASSERT(!transport.sendFrame(TEST_CMD_ID, payload, sizeof(payload)));
}

static void test_transport_sendFrame_fails_on_short_write() {
  VectorStream stream;
  stream.mode = WriteMode::ShortAlways;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(rpc::RPC_DEFAULT_BAUDRATE);

  const uint8_t payload[] = {
      rpc::RPC_TEST_PAYLOAD_BYTE,
      rpc::RPC_TEST_MARKER_BYTE,
      rpc::RPC_TEST_EXIT_CODE,
  };
  TEST_ASSERT(!transport.sendFrame(TEST_CMD_ID, payload, sizeof(payload)));
}

static void test_transport_sendFrame_fails_when_terminator_write_fails() {
  VectorStream stream;
  stream.mode = WriteMode::TerminatorFailsOnSecondCall;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(rpc::RPC_DEFAULT_BAUDRATE);

  const uint8_t payload[] = {rpc::RPC_TEST_PAYLOAD_BYTE};
  TEST_ASSERT(!transport.sendFrame(TEST_CMD_ID, payload, sizeof(payload)));
}

static void test_transport_sendControlFrame_fails_when_terminator_write_fails() {
  VectorStream stream;
  stream.mode = WriteMode::TerminatorFailsOnSecondCall;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(rpc::RPC_DEFAULT_BAUDRATE);

  TEST_ASSERT(!transport.sendControlFrame(rpc::to_underlying(rpc::CommandId::CMD_XOFF)));
}

static void test_transport_retransmitLastFrame_behaviors() {
  VectorStream stream;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(rpc::RPC_DEFAULT_BAUDRATE);

  // No last frame yet.
  TEST_ASSERT(!transport.retransmitLastFrame());

  // Create a last frame.
  const uint8_t payload[] = {rpc::RPC_TEST_PAYLOAD_BYTE, rpc::RPC_TEST_MARKER_BYTE};
  TEST_ASSERT(transport.sendFrame(TEST_CMD_ID, payload, sizeof(payload)));
  TEST_ASSERT(transport._last_cobs_len > 0);

  // Now force a terminator failure to hit the error branch.
  stream.mode = WriteMode::TerminatorFailsOnSecondCall;
  stream.buffer_write_calls = 0;
  TEST_ASSERT(!transport.retransmitLastFrame());
}

static void test_transport_processInput_flow_control_pause_resume() {
  VectorStream stream;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(rpc::RPC_DEFAULT_BAUDRATE);

  // Add enough bytes so available() trips the high-water mark.
  uint8_t inbound[50];
  test_memfill(inbound, sizeof(inbound), rpc::RPC_TEST_MARKER_BYTE);
  stream.inject_rx(inbound, sizeof(inbound));

  rpc::Frame frame{};
  TEST_ASSERT(!transport.processInput(frame));
  TEST_ASSERT(transport.isFlowPaused());
  TEST_ASSERT(stream.tx.len > 0);
  TEST_ASSERT(stream.tx.data[stream.tx.len - 1] == rpc::RPC_FRAME_DELIMITER);

  // On next call, buffer is drained (available == 0), should resume flow.
  stream.clear_tx();
  TEST_ASSERT(!transport.processInput(frame));
  TEST_ASSERT(!transport.isFlowPaused());
  TEST_ASSERT(stream.tx.len > 0);
  TEST_ASSERT(stream.tx.data[stream.tx.len - 1] == rpc::RPC_FRAME_DELIMITER);
}

static void test_transport_processInput_overflow_sets_error() {
  VectorStream stream;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(rpc::RPC_DEFAULT_BAUDRATE);

  // Feed more than the FrameParser buffer without a delimiter.
  enum { kLen = rpc::COBS_BUFFER_SIZE + 1 };
  uint8_t inbound[kLen];
  test_memfill(inbound, sizeof(inbound), rpc::RPC_TEST_MARKER_BYTE);
  stream.inject_rx(inbound, sizeof(inbound));

  rpc::Frame frame{};
  TEST_ASSERT(!transport.processInput(frame));
  TEST_ASSERT(transport.hasOverflowed());
  TEST_ASSERT(transport.getLastError() == rpc::FrameParser::Error::OVERFLOW);
}

}  // namespace

int main() {
  test_cobs_null_guards();
  test_transport_sendFrame_rejects_oversized_payload();
  test_transport_sendFrame_fails_on_short_write();
  test_transport_sendFrame_fails_when_terminator_write_fails();
  test_transport_sendControlFrame_fails_when_terminator_write_fails();
  test_transport_retransmitLastFrame_behaviors();
  test_transport_processInput_flow_control_pause_resume();
  test_transport_processInput_overflow_sets_error();

  return 0;
}
