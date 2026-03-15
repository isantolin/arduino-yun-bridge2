#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define ARDUINO_STUB_CUSTOM_MILLIS 1
#include "Bridge.h"
#include "BridgeTestHelper.h"
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

// Helper to build a Nanopb frame and feed it
template <typename T>
void feed_pb_frame(uint16_t cmd, const T& msg) {
  uint8_t payload_buf[rpc::MAX_PAYLOAD_SIZE];
  pb_ostream_t out_stream = pb_ostream_from_buffer(payload_buf, sizeof(payload_buf));
  if (pb_encode(&out_stream, rpc::Payload::Descriptor<T>::fields(), &msg)) {
    feed_frame(cmd, payload_buf, out_stream.bytes_written);
  }
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
  
  rpc::payload::PinRead pr_msg = mcubridge_PinRead_init_default;
  pr_msg.pin = pin;
  feed_pb_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_READ), pr_msg);
  Bridge.process();
  
  feed_pb_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_READ), pr_msg);
  Bridge.process();
  
  rpc::payload::DigitalWrite dw_msg = mcubridge_DigitalWrite_init_default;
  dw_msg.pin = pin;
  dw_msg.value = 1;
  feed_pb_frame(rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), dw_msg);
  Bridge.process();
  
  rpc::payload::AnalogWrite aw_msg = mcubridge_AnalogWrite_init_default;
  aw_msg.pin = pin;
  aw_msg.value = 128;
  feed_pb_frame(rpc::to_underlying(rpc::CommandId::CMD_ANALOG_WRITE), aw_msg);
  Bridge.process();
}

void test_bridge_status_gaps() {
  printf("  -> bridge_status_gaps\n");
  auto ba = TestAccessor::create(Bridge);
  
  char long_msg[1024];
  memset(long_msg, 'A', 1023);
  long_msg[1023] = '\0';
  ba.setIdle();
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view(long_msg));
  
  // Status ACK (Line 482-483)
  rpc::payload::AckPacket ack_msg = mcubridge_AckPacket_init_default;
  ack_msg.command_id = rpc::to_underlying(rpc::CommandId::CMD_GET_VERSION);
  feed_pb_frame(rpc::to_underlying(rpc::StatusCode::STATUS_ACK), ack_msg);
  Bridge.process();
}

void test_bridge_send_gaps() {
  printf("  -> bridge_send_gaps\n");
  // Test sendPbCommand with various payloads (replaces removed sendStringCommand/sendKeyValCommand)
  rpc::payload::DatastorePut put_msg = mcubridge_DatastorePut_init_zero;
  Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_PUT, put_msg);
  rpc::payload::DatastoreGet get_msg = mcubridge_DatastoreGet_init_zero;
  Bridge.sendPbCommand(rpc::CommandId::CMD_DATASTORE_GET, get_msg);
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
  // In host tests, isValidPin always returns true (no NUM_DIGITAL_PINS)
  TEST_ASSERT(bridge::hal::isValidPin(30) == true);
}

void setUp(void) {}
void tearDown(void) {}

int main(void) {
  Bridge.begin(115200);
  UNITY_BEGIN();
  RUN_TEST(test_bridge_process_gaps);
  RUN_TEST(test_bridge_gpio_gaps);
  RUN_TEST(test_bridge_status_gaps);
  RUN_TEST(test_bridge_send_gaps);
  RUN_TEST(test_fsm_internal_gaps);
  RUN_TEST(test_security_cpp_gaps);
  RUN_TEST(test_hal_gaps);
  return UNITY_END();
}
