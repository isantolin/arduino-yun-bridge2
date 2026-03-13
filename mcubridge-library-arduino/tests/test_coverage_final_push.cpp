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

BiStream g_mock_stream;
Stream* g_arduino_stream_delegate = &g_mock_stream;

HardwareSerial Serial;
HardwareSerial Serial1;

static unsigned long g_test_millis = 0;
unsigned long millis() { return g_test_millis; }

// Global instances required by Bridge.cpp linkage
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

// ONLY ONE DEFINITION OF BRIDGE
BridgeClass Bridge(g_mock_stream);

using namespace bridge::fsm;
using namespace bridge::test;
using namespace bridge::router;

// Helper to build a COBS frame and feed it
void feed_frame(uint16_t cmd, const uint8_t* payload, size_t len, bool corrupt_crc = false, uint8_t ver = rpc::PROTOCOL_VERSION) {
  uint8_t raw[1024];
  raw[0] = ver;
  rpc::write_u16_be(etl::span<uint8_t>(&raw[1], 2), static_cast<uint16_t>(len));
  rpc::write_u16_be(etl::span<uint8_t>(&raw[3], 2), cmd);
  if (payload && len > 0) {
    memcpy(&raw[5], payload, len);
  }
  
  size_t data_len = 5 + len;
  etl::crc32 crc_calc;
  crc_calc.add(raw, raw + data_len);
  uint32_t crc = crc_calc.value();
  if (corrupt_crc) crc ^= 0xFFFFFFFF;
  rpc::write_u32_be(etl::span<uint8_t>(&raw[data_len], 4), crc);
  
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

void test_bridge_core_gaps() {
  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();

  // 1. POST failure paths (Line 154-156, 165-167)
  // Since we can't easily fail memory POST without real hardware, we call the result directly
  Bridge.enterSafeState();
  ba.fsmCryptoFault();
  
  // 2. Process / COBS gaps
  ba.setUnsynchronized(); // switch statement gap
  
  // CRC mismatch (Line 270)
  feed_frame(0, nullptr, 0, true);
  Bridge.process();
  
  // Bad version (Line 267)
  feed_frame(0, nullptr, 0, false, 0x99);
  Bridge.process();
  
  // Malformed COBS (Line 273)
  uint8_t malformed[] = {0x02, 0x01, 0x00};
  g_mock_stream.feed(malformed, 3);
  Bridge.process();

  // Buffer Overflow (Line 296-297, 306-307)
  uint8_t overflow[1100];
  memset(overflow, 0x01, 1100);
  g_mock_stream.feed(overflow, 1100);
  Bridge.process();
}

void test_bridge_router_gaps() {
  auto ba = TestAccessor::create(Bridge);
  ba.setIdle();

  // GPIO Errors (Line 615, 617, 638, 650)
  uint8_t bad_pin = 25; 
  uint8_t pl[2] = {bad_pin, 1};
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ), pl, 1);
  Bridge.process();
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ), pl, 1);
  Bridge.process();
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), pl, 2);
  Bridge.process();
  feed_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE), pl, 2);
  Bridge.process();

  // Status Router gaps
  feed_frame(rpc::to_underlying(rpc::StatusCode::STATUS_MALFORMED), nullptr, 0);
  Bridge.process();
  
  uint8_t ack_pl[2];
  rpc::write_u16_be(etl::span<uint8_t>(ack_pl, 2), rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION));
  feed_frame(rpc::to_underlying(rpc::StatusCode::STATUS_ACK), ack_pl, 2);
  Bridge.process();
}

void test_bridge_internal_logic_gaps() {
  auto ba = TestAccessor::create(Bridge);
  
  // consecutive CRC errors -> safe state (Line 330)
  ba.setIdle();
  for (int i = 0; i < 15; ++i) {
    ba.setLastParseError(rpc::FrameError::CRC_MISMATCH);
    Bridge.process();
  }
  
  // onRxDedupe (Line 1260)
  ba.onRxDedupe();
  
  // onStartupStabilized (Line 1276)
  ba.setStartupStabilizing(true);
  ba.onStartupStabilized();
  
  // computeHandshakeTag (Line 1385-1393)
  uint8_t nonce[16] = {0};
  uint8_t tag[32];
  uint8_t secret[] = "secret";
  ba.assignSharedSecret(secret, secret + 6);
  ba.computeHandshakeTag(nonce, 16, tag);
  
  // applyTimingConfig (Line 1434-1437)
  uint8_t timing_pl[8] = {0, 100, 5, 0, 0, 0, 200};
  ba.applyTimingConfig(timing_pl, 8);
}

void test_bridge_send_gaps() {
  // sendStringCommand overflow (Line 1267-1268)
  char long_str[1024];
  memset(long_str, 'A', 1023);
  long_str[1023] = '\0';
  Bridge.sendStringCommand(rpc::CommandId::CMD_CONSOLE_WRITE, long_str, 10);
  
  // sendKeyValCommand overflow (Line 1286)
  Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_GET_RESP, "key", 2, "val", 10);
  Bridge.sendKeyValCommand(rpc::CommandId::CMD_DATASTORE_GET_RESP, "key", 10, "val", 2);
}

void test_fsm_internal_gaps() {
  BridgeFsm fsm;
  fsm.begin();
  fsm.resetFsm();
  fsm.handshakeStart();
  fsm.handshakeComplete();
  fsm.sendCritical();
  fsm.handshakeFailed();
  fsm.cryptoFault();
  
  fsm.resetFsm();
  fsm.handshakeStart();
  fsm.resetFsm();
}

void test_security_cpp_gaps() {
  // KAT failures (simulated as branches)
  rpc::security::run_cryptographic_self_tests();
}

void test_hal_gaps() {
  // isValidPin (Line 39, 41)
  TEST_ASSERT(bridge::hal::isValidPin(10) == true);
  TEST_ASSERT(bridge::hal::isValidPin(30) == false);
  bridge::hal::init();
}

void test_subsystems_gaps() {
  // Console gaps (Line 40, 63)
  Console.begin();
  auto ca = ConsoleTestAccessor::create(Console);
  ca.setBegun(false);
  Console.write('A'); // begun=false
  ca.setBegun(true);
  
  auto ba = TestAccessor::create(Bridge);
  ba.setUnsynchronized(); // for flush
  
  // Process gaps (Line 17, 36-37, 63, 71)
#if BRIDGE_ENABLE_PROCESS
  Process.runAsync("");
  Process.poll(-1);
  Process.kill(-1);
  Process.reset();
  auto pa = ProcessTestAccessor::create(Process);
  pa.pushPendingPid(123);
  Process.poll(123);
#endif
}

void test_structs_gaps() {
  // Cover all encode/parse in rpc_structs.h
  uint8_t buf[256];
  rpc::payload::VersionResponse{1, 1}.encode(buf);
  rpc::payload::FreeMemoryResponse{4096}.encode(buf);
  rpc::payload::Capabilities{1, 1, 1, 1, 1}.encode(buf);
  rpc::payload::PinMode{1, 1}.encode(buf);
  rpc::payload::DigitalWrite{1, 1}.encode(buf);
  rpc::payload::AnalogWrite{1, 1}.encode(buf);
  rpc::payload::PinRead{1}.encode(buf);
  rpc::payload::DigitalReadResponse{1}.encode(buf);
  rpc::payload::AnalogReadResponse{1023}.encode(buf);
  rpc::payload::MailboxProcessed{1}.encode(buf);
  rpc::payload::MailboxAvailableResponse{1}.encode(buf);
  rpc::payload::ProcessKill{1}.encode(buf);
  rpc::payload::ProcessPoll{1}.encode(buf);
  rpc::payload::ProcessRunAsyncResponse{1}.encode(buf);
  rpc::payload::AckPacket{1}.encode(buf);
  rpc::payload::HandshakeConfig{1, 1, 1}.encode(buf);
  rpc::payload::SetBaudratePacket{115200}.encode(buf);
  
  rpc::payload::VersionResponse::parse(buf);
  rpc::payload::FreeMemoryResponse::parse(buf);
  rpc::payload::Capabilities::parse(buf);
  rpc::payload::PinMode::parse(buf);
  rpc::payload::DigitalWrite::parse(buf);
  rpc::payload::AnalogWrite::parse(buf);
  rpc::payload::PinRead::parse(buf);
  rpc::payload::DigitalReadResponse::parse(buf);
  rpc::payload::AnalogReadResponse::parse(buf);
  rpc::payload::MailboxProcessed::parse(buf);
  rpc::payload::MailboxAvailableResponse::parse(buf);
  rpc::payload::ProcessKill::parse(buf);
  rpc::payload::ProcessPoll::parse(buf);
  rpc::payload::ProcessRunAsyncResponse::parse(buf);
  rpc::payload::AckPacket::parse(buf);
  rpc::payload::HandshakeConfig::parse(buf);
  rpc::payload::SetBaudratePacket::parse(buf);
}

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  Bridge.begin(115200);
  UNITY_BEGIN();
  RUN_TEST(test_bridge_core_gaps);
  RUN_TEST(test_bridge_router_gaps);
  RUN_TEST(test_bridge_internal_logic_gaps);
  RUN_TEST(test_bridge_send_gaps);
  RUN_TEST(test_fsm_internal_gaps);
  RUN_TEST(test_security_cpp_gaps);
  RUN_TEST(test_hal_gaps);
  RUN_TEST(test_subsystems_gaps);
  RUN_TEST(test_structs_gaps);
  return UNITY_END();
}
