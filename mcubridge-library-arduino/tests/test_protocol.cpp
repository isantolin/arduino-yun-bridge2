#define BRIDGE_HOST_TEST 1
#include <Arduino.h>
#include <etl/array.h>
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
  etl::array<uint8_t, 4> small_buf;
  Frame f = {};
  f.payload = etl::span<const uint8_t>();
  TEST_ASSERT_EQUAL(
      0, FrameParser::serialize(f, etl::span<uint8_t>(small_buf.data(), 4)));

  // 4. FrameParser::parse error paths
  FrameParser parser;
  etl::array<uint8_t, 32> raw;

  // Malformed: too short
  TEST_ASSERT(!parser.parse(etl::span<const uint8_t>(raw.data(), 2)).has_value());

  // Malformed: wrong version
  raw.fill(0);
  raw[0] = 0xFF;  // Bad version
  TEST_ASSERT(
      !parser.parse(etl::span<const uint8_t>(raw.data(), MIN_FRAME_SIZE)).has_value());

  // Malformed: payload length mismatch
  raw.fill(0);
  raw[0] = PROTOCOL_VERSION;
  raw[1] = 0;
  raw[2] = 10;  // Length 10 but buffer only MIN_FRAME_SIZE
  TEST_ASSERT(
      !parser.parse(etl::span<const uint8_t>(raw.data(), MIN_FRAME_SIZE)).has_value());

  // CRC Mismatch
  etl::array<uint8_t, MIN_FRAME_SIZE> valid;
  valid.fill(0);
  valid[0] = PROTOCOL_VERSION;
  // payload_length = 0 (valid[1]=0, valid[2]=0)
  // command_id = 0 (valid[3]=0, valid[4]=0)
  // sequence_id = 0 (valid[5]=0, valid[6]=0)
  // CRC trailer starts at index 7
  valid[MIN_FRAME_SIZE - 1] = 0xFF; // Break CRC
  auto res = parser.parse(etl::span<const uint8_t>(valid));
  TEST_ASSERT(!res.has_value());
  TEST_ASSERT(res.error() == FrameError::CRC_MISMATCH);
}

void test_protocol_builder_exhaustive() {
  using namespace rpc;
  etl::array<uint8_t, 128> buf;
  etl::array<uint8_t, 3> payload = {1, 2, 3};

  // Success path
  size_t len = FrameBuilder::build(etl::span<uint8_t>(buf.data(), 128),
                                   (uint16_t)CommandId::CMD_GET_VERSION, 1,
                                   etl::span<const uint8_t>(payload.data(), 3));
  TEST_ASSERT(len > 0);

  // Buffer too small
  len = FrameBuilder::build(etl::span<uint8_t>(buf.data(), 5),
                            (uint16_t)CommandId::CMD_GET_VERSION, 1,
                            etl::span<const uint8_t>(payload.data(), 3));
  TEST_ASSERT_EQUAL(0, len);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_protocol_frame_logic_exhaustive);
  RUN_TEST(test_protocol_builder_exhaustive);
  return UNITY_END();
}
