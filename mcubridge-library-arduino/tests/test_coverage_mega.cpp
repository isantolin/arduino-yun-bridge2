#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "fsm/bridge_fsm.h"
#include "protocol/rle.h"
#include "protocol/rpc_frame.h"
#include "protocol/rpc_protocol.h"
#include "protocol/rpc_structs.h"
#include "router/command_router.h"
#include "security/security.h"
#include "test_support.h"

#define BRIDGE_ENABLE_TEST_INTERFACE 1
#include "BridgeTestInterface.h"

// --- Mock Stream Implementation ---
class MockStream : public Stream {
 public:
  ByteBuffer<4096> rx_buf;
  ByteBuffer<4096> tx_buf;

  int available() override { return rx_buf.remaining(); }
  int read() override { return rx_buf.read_byte(); }
  int peek() override { return rx_buf.peek_byte(); }
  size_t write(uint8_t b) override {
    tx_buf.push(b);
    return 1;
  }
  void flush() override {}

  void feed(const uint8_t* data, size_t len) { rx_buf.append(data, len); }
};

MockStream g_mock_stream;
Stream* g_arduino_stream_delegate = &g_mock_stream;

HardwareSerial Serial;
HardwareSerial Serial1;

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

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

BridgeClass Bridge(g_mock_stream);

using namespace bridge::fsm;
using namespace bridge::test;
using namespace bridge::router;

// Helper to build a COBS frame and feed it
void feed_frame(uint16_t cmd, const uint8_t* payload, size_t len, bool corrupt_crc = false, uint8_t ver = rpc::PROTOCOL_VERSION) {
  uint8_t raw[1024];
  raw[0] = ver;
  rpc::write_u16_be(&raw[1], static_cast<uint16_t>(len));
  rpc::write_u16_be(&raw[3], cmd);
  if (payload && len > 0) {
    memcpy(&raw[5], payload, len);
  }
  
  size_t data_len = 5 + len;
  etl::crc32 crc_calc;
  crc_calc.add(raw, raw + data_len);
  uint32_t crc = crc_calc.value();
  if (corrupt_crc) crc ^= 0xFFFFFFFF;
  rpc::write_u32_be(&raw[data_len], crc);
  
  size_t total_raw = data_len + 4;
  
  // Simple COBS-like encoding (assuming no 0x00 in raw)
  uint8_t cobs[2048];
  uint8_t* dst = cobs;
  *dst++ = static_cast<uint8_t>(total_raw + 1);
  for(size_t i=0; i<total_raw; ++i) {
      *dst++ = (raw[i] == 0) ? 0x01 : raw[i];
  }
  *dst++ = 0x00;
  
  g_mock_stream.feed(cobs, dst - cobs);
}

void test_bridge_process_gaps() {
  printf("  -> bridge_process_gaps\n");
  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();

  // 1. Wrong CRC (Line 270)
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION), nullptr, 0, true);
  Bridge.process();
  
  // 2. Wrong Version (Line 267)
  uint8_t bad_ver_raw[10] = {0x03, 0, 0, 0, 0, 0, 0, 0, 0, 0};
  uint8_t bad_ver_cobs[12] = {11, 0x03, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0};
  g_mock_stream.feed(bad_ver_cobs, 12);
  Bridge.process();

  // 3. Buffer Overflow (Line 296-297, 306-307)
  uint8_t overflow[1100];
  memset(overflow, 0x01, 1100);
  g_mock_stream.feed(overflow, 1100);
  Bridge.process();
  
  // 4. Malformed COBS (Line 273)
  uint8_t malformed[] = {0x02, 0x01, 0x00};
  g_mock_stream.feed(malformed, 3);
  Bridge.process();
}

void test_bridge_gpio_gaps() {
  printf("  -> bridge_gpio_gaps\n");
  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();

  uint8_t pin = 25; 
  uint8_t pl[2] = {pin, 1};
  
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ), pl, 1);
  Bridge.process();
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ), pl, 1);
  Bridge.process();
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), pl, 2);
  Bridge.process();
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE), pl, 2);
  Bridge.process();
}

void test_bridge_status_gaps() {
  printf("  -> bridge_status_gaps\n");
  auto ba = TestAccessor::create(Bridge);
  
  char long_msg[1024];
  memset(long_msg, 'A', 1023);
  long_msg[1023] = '\0';
  ba.setIdle();
  Bridge._emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view(long_msg));
  
  // Status ACK (Line 482-483)
  uint8_t ack_pl[2];
  rpc::write_u16_be(ack_pl, rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION));
  feed_frame(rpc::to_underlying(rpc::StatusCode::STATUS_ACK), ack_pl, 2);
  Bridge.process();
}

void test_bridge_send_gaps() {
  printf("  -> bridge_send_gaps\n");
  Bridge.sendStringCommand(rpc::CommandId::CMD_CONSOLE_WRITE, "too long string", 5);
  Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_GET_RESP, "key", 2, "val", 10);
  Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_GET_RESP, "key", 10, "val", 2);
}

void test_fsm_internal_gaps() {
  printf("  -> fsm_internal_gaps\n");
  BridgeFsm fsm_obj;
  fsm_obj.begin();
  fsm_obj.resetFsm();
  fsm_obj.handshakeStart();
  fsm_obj.handshakeComplete();
  fsm_obj.sendCritical();
  fsm_obj.handshakeFailed();
  fsm_obj.cryptoFault();
  
  fsm_obj.resetFsm();
  fsm_obj.handshakeStart();
  fsm_obj.resetFsm();
}

void test_security_cpp_gaps() {
  printf("  -> security_cpp_gaps\n");
  rpc::security::run_cryptographic_self_tests();
}

void test_hal_gaps() {
  printf("  -> hal_gaps\n");
  TEST_ASSERT(bridge::hal::isValidPin(10) == true);
  TEST_ASSERT(bridge::hal::isValidPin(30) == false);
}

int main() {
  printf("COVERAGE MEGA TEST START\n");
  Bridge.begin(115200);

  test_bridge_process_gaps();
  test_bridge_gpio_gaps();
  test_bridge_status_gaps();
  test_bridge_send_gaps();
  test_fsm_internal_gaps();
  test_security_cpp_gaps();
  test_hal_gaps();

  printf("COVERAGE MEGA TEST END\n");
  return 0;
}
