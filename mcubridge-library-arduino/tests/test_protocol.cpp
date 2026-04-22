#define BRIDGE_HOST_TEST 1
#include <Arduino.h>
#include <unity.h>

#include "protocol/rpc_frame.h"

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

void setUp(void) {}
void tearDown(void) {}

void test_protocol_frame_logic_exhaustive() {
  using namespace rpc;

  // 1. is_reliable exhaustive
  TEST_ASSERT(is_reliable((uint16_t)CommandId::CMD_CONSOLE_WRITE));
  TEST_ASSERT(!is_reliable((uint16_t)CommandId::CMD_GET_VERSION));

  // 2. is_compressed
  TEST_ASSERT(is_compressed(0x8000));
  TEST_ASSERT(!is_compressed(0x0001));

  // 3. FrameParser::serialize error paths (buffer too small)
  uint8_t small_buf[4];
  Frame f = {};
  f.payload = etl::span<const uint8_t>();
  TEST_ASSERT_EQUAL(
      0, FrameParser::serialize(f, etl::span<uint8_t>(small_buf, 4)));

  // 4. FrameParser::parse error paths
  FrameParser parser;
  uint8_t raw[32];

  // Malformed: too short
  TEST_ASSERT(!parser.parse(etl::span<const uint8_t>(raw, 2)).has_value());

  // Malformed: wrong version
  memset(raw, 0, 32);
  raw[0] = 0xFF;  // Bad version
  TEST_ASSERT(
      !parser.parse(etl::span<const uint8_t>(raw, MIN_FRAME_SIZE)).has_value());

  // Malformed: payload length mismatch
  memset(raw, 0, 32);
  raw[0] = PROTOCOL_VERSION;
  raw[1] = 0;
  raw[2] = 10;  // Length 10 but buffer only MIN_FRAME_SIZE
  TEST_ASSERT(
      !parser.parse(etl::span<const uint8_t>(raw, MIN_FRAME_SIZE)).has_value());
}

void test_protocol_builder_exhaustive() {
  using namespace rpc;
  uint8_t buf[128];
  uint8_t payload[] = {1, 2, 3};

  // Success path
  size_t len = FrameBuilder::build(etl::span<uint8_t>(buf, 128),
                                   (uint16_t)CommandId::CMD_GET_VERSION, 1,
                                   etl::span<const uint8_t>(payload, 3));
  TEST_ASSERT(len > 0);

  // Buffer too small
  len = FrameBuilder::build(etl::span<uint8_t>(buf, 5),
                            (uint16_t)CommandId::CMD_GET_VERSION, 1,
                            etl::span<const uint8_t>(payload, 3));
  TEST_ASSERT_EQUAL(0, len);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_protocol_frame_logic_exhaustive);
  RUN_TEST(test_protocol_builder_exhaustive);
  return UNITY_END();
}
