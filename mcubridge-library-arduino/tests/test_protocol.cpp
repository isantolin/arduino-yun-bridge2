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

  // 1. requires_ack exhaustive
  TEST_ASSERT(requires_ack((uint16_t)CommandId::CMD_CONSOLE_WRITE));
  TEST_ASSERT(!requires_ack((uint16_t)CommandId::CMD_GET_VERSION));

  // 2. is_compressed
  TEST_ASSERT(rpc::is_compressed(rpc::RPC_CMD_FLAG_COMPRESSED));
  TEST_ASSERT(!rpc::is_compressed(0x0001));

  // 3. FrameParser::serialize error paths (buffer too small)
  etl::array<uint8_t, 2> small_buf;
  Frame f = {};
  TEST_ASSERT_EQUAL(
      0, FrameParser::serialize(f, etl::span<uint8_t>(small_buf.data(), 2)));

  // 4. FrameParser::parse error paths
  etl::array<uint8_t, 128> raw;
  raw.fill(0);

  // Malformed: too short
  TEST_ASSERT(!FrameParser::parse(etl::span<const uint8_t>(raw.data(), 2)).has_value());

  // Malformed: wrong version
  Frame f_valid;
  f_valid.envelope.version = 0xFF;
  size_t v_len = FrameParser::serialize(f_valid, raw);
  TEST_ASSERT(!FrameParser::parse(etl::span<const uint8_t>(raw.data(), v_len)).has_value());

  // CRC Mismatch
  f_valid.envelope.version = PROTOCOL_VERSION;
  v_len = FrameParser::serialize(f_valid, raw);
  TEST_ASSERT(v_len > 0);
  raw[v_len - 1] ^= 0xFF; // Break CRC

  auto res = FrameParser::parse(etl::span<const uint8_t>(raw.data(), v_len));
  TEST_ASSERT(!res.has_value());
  TEST_ASSERT(res.error() == FrameError::CRC_MISMATCH);

  // Correct CRC
  raw[v_len - 1] ^= 0xFF; // Restore CRC
  TEST_ASSERT(FrameParser::parse(etl::span<const uint8_t>(raw.data(), v_len)).has_value());
}

void test_protocol_builder_exhaustive() {
  using namespace rpc;
  etl::array<uint8_t, 128> buf;
  etl::array<uint8_t, 3> payload = {1, 2, 3};
  etl::array<uint8_t, 12> nonce = {};
  etl::array<uint8_t, 16> tag = {};

  // Success path
  size_t len = FrameBuilder::build(etl::span<uint8_t>(buf.data(), 128),
                                   (uint16_t)CommandId::CMD_GET_VERSION, 1,
                                   etl::span<const uint8_t>(payload.data(), 3),
                                   nonce, tag);
  TEST_ASSERT(len > 0);

  // Buffer too small
  len = FrameBuilder::build(etl::span<uint8_t>(buf.data(), 2),
                            (uint16_t)CommandId::CMD_GET_VERSION, 1,
                            etl::span<const uint8_t>(payload.data(), 3),
                            nonce, tag);
  TEST_ASSERT_EQUAL(0, len);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_protocol_frame_logic_exhaustive);
  RUN_TEST(test_protocol_builder_exhaustive);
  return UNITY_END();
}
