#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <vector>

// Must be defined before including Bridge.h to expose private fields on host.
#define BRIDGE_HOST_TEST 1

#include "Bridge.h"
#include "arduino/BridgeTransport.h"
#include "protocol/cobs.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"

using namespace rpc;

#define TEST_ASSERT(cond) \
  do { \
    if (!(cond)) { \
      std::cerr << "[FATAL] Assertion failed at line " << __LINE__ << ": " \
                << #cond << std::endl; \
      std::abort(); \
    } \
  } while (0)

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
  std::vector<uint8_t> tx;
  std::vector<uint8_t> rx;
  size_t rx_pos = 0;

  WriteMode mode = WriteMode::Normal;
  int buffer_write_calls = 0;

  size_t write(uint8_t c) override {
    tx.push_back(c);
    return 1;
  }

  size_t write(const uint8_t* buffer, size_t size) override {
    buffer_write_calls++;

    if (!buffer || size == 0) {
      return 0;
    }

    if (mode == WriteMode::ShortAlways) {
      const size_t n = (size > 0) ? (size - 1) : 0;
      tx.insert(tx.end(), buffer, buffer + n);
      return n;
    }

    if (mode == WriteMode::TerminatorFailsOnSecondCall && buffer_write_calls >= 2) {
      // Simulate a missing terminator write.
      return 0;
    }

    tx.insert(tx.end(), buffer, buffer + size);
    return size;
  }

  int available() override {
    return static_cast<int>(rx.size() - rx_pos);
  }

  int read() override {
    if (rx_pos >= rx.size()) return -1;
    return rx[rx_pos++];
  }

  int peek() override {
    if (rx_pos >= rx.size()) return -1;
    return rx[rx_pos];
  }

  void flush() override {}

  void inject_rx(const std::vector<uint8_t>& data) {
    rx.insert(rx.end(), data.begin(), data.end());
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
  transport.begin(115200);

  std::vector<uint8_t> payload(rpc::MAX_PAYLOAD_SIZE + 1, 0xAB);
  TEST_ASSERT(!transport.sendFrame(0x1234, payload.data(), payload.size()));
}

static void test_transport_sendFrame_fails_on_short_write() {
  VectorStream stream;
  stream.mode = WriteMode::ShortAlways;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(115200);

  const uint8_t payload[] = {0x01, 0x02, 0x03};
  TEST_ASSERT(!transport.sendFrame(0x1234, payload, sizeof(payload)));
}

static void test_transport_sendFrame_fails_when_terminator_write_fails() {
  VectorStream stream;
  stream.mode = WriteMode::TerminatorFailsOnSecondCall;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(115200);

  const uint8_t payload[] = {0xAA};
  TEST_ASSERT(!transport.sendFrame(0x1234, payload, sizeof(payload)));
}

static void test_transport_sendControlFrame_fails_when_terminator_write_fails() {
  VectorStream stream;
  stream.mode = WriteMode::TerminatorFailsOnSecondCall;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(115200);

  TEST_ASSERT(!transport.sendControlFrame(rpc::to_underlying(rpc::CommandId::CMD_XOFF)));
}

static void test_transport_retransmitLastFrame_behaviors() {
  VectorStream stream;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(115200);

  // No last frame yet.
  TEST_ASSERT(!transport.retransmitLastFrame());

  // Create a last frame.
  const uint8_t payload[] = {0x10, 0x20};
  TEST_ASSERT(transport.sendFrame(0x2222, payload, sizeof(payload)));
  TEST_ASSERT(transport._last_cobs_len > 0);

  // Now force a terminator failure to hit the error branch.
  stream.mode = WriteMode::TerminatorFailsOnSecondCall;
  stream.buffer_write_calls = 0;
  TEST_ASSERT(!transport.retransmitLastFrame());
}

static void test_transport_processInput_flow_control_pause_resume() {
  VectorStream stream;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(115200);

  // Add enough bytes so available() trips the high-water mark.
  stream.inject_rx(std::vector<uint8_t>(50, 0x11));

  rpc::Frame frame{};
  TEST_ASSERT(!transport.processInput(frame));
  TEST_ASSERT(transport.isFlowPaused());
  TEST_ASSERT(!stream.tx.empty());
  TEST_ASSERT(stream.tx.back() == rpc::RPC_FRAME_DELIMITER);

  // On next call, buffer is drained (available == 0), should resume flow.
  stream.clear_tx();
  TEST_ASSERT(!transport.processInput(frame));
  TEST_ASSERT(!transport.isFlowPaused());
  TEST_ASSERT(!stream.tx.empty());
  TEST_ASSERT(stream.tx.back() == rpc::RPC_FRAME_DELIMITER);
}

static void test_transport_processInput_overflow_sets_error() {
  VectorStream stream;
  bridge::BridgeTransport transport(stream, nullptr);
  transport.begin(115200);

  // Feed more than the FrameParser buffer without a delimiter.
  stream.inject_rx(std::vector<uint8_t>(rpc::COBS_BUFFER_SIZE + 1, 0x55));

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

  std::cout << "BridgeTransport tests passed" << std::endl;
  return 0;
}
