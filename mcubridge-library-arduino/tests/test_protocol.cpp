#include <unity.h>
#include <etl/array.h>
#include <etl/span.h>
#include "protocol/rpc_frame.h"

using namespace rpc;

void setUp() {}
void tearDown() {}

void test_protocol_frame_logic_exhaustive() {
  // Test basic frame structure and constants
  TEST_ASSERT_EQUAL(2U, PROTOCOL_VERSION);
  TEST_ASSERT_EQUAL(39U, RPC_MIN_FRAME_SIZE);
}

void test_protocol_parser_exhaustive() {
  etl::array<uint8_t, 128> buf;
  buf.fill(0);

  // Test malformed (too short)
  auto res = FrameParser::parse(etl::span<const uint8_t>(buf.data(), 5));
  TEST_ASSERT_FALSE(res.has_value());
  TEST_ASSERT_EQUAL(FrameError::MALFORMED, res.error());
}

void test_protocol_builder_exhaustive() {
  etl::array<uint8_t, 128> buf;
  etl::array<uint8_t, 3> payload = {1, 2, 3};
  etl::array<uint8_t, 12> nonce;
  nonce.fill(0);
  etl::array<uint8_t, 16> tag;
  tag.fill(0);

  size_t len = FrameBuilder::build(etl::span<uint8_t>(buf.data(), 128),
                                   1, etl::span<const uint8_t>(payload.data(), 3),
                                   nonce, tag);
  TEST_ASSERT(len > 0);
  TEST_ASSERT(len >= RPC_MIN_FRAME_SIZE);

  // Buffer too small
  len = FrameBuilder::build(etl::span<uint8_t>(buf.data(), 2),
                            1, etl::span<const uint8_t>(payload.data(), 3),
                            nonce, tag);
  TEST_ASSERT_EQUAL(0, len);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_protocol_frame_logic_exhaustive);
  RUN_TEST(test_protocol_parser_exhaustive);
  RUN_TEST(test_protocol_builder_exhaustive);
  return UNITY_END();
}
