#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "Bridge.h"
#include "protocol/rpc_frame.h"
#include "services/SPIService.h"
#include "test_constants.h"
#include "test_support.h"

using namespace rpc;

// Define the global delegates and stubs for HardwareSerial stub
Stream* g_arduino_stream_delegate = nullptr;
HardwareSerial Serial;
HardwareSerial Serial1;

// Unity setup/teardown
void setUp(void) {}
void tearDown(void) {}

// 1. Helpers de Endianness
static void test_endianness_helpers() {
  uint8_t buffer[8] = {0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0};
  TEST_ASSERT(read_u16_be(etl::span<const uint8_t>(buffer, 2)) == 0x1234);
  TEST_ASSERT(read_u64_be(etl::span<const uint8_t>(buffer, 8)) == 0x123456789ABCDEF0ULL);

  uint8_t out[8] = {0};
  write_u64_be(etl::span<uint8_t>(out, 8), 0xDEADBEEFCAFEBABEULL);
  TEST_ASSERT(out[0] == 0xDE && out[7] == 0xBE);
}

// 2. CRC Helpers
static void test_crc_helpers() {
  const uint8_t data[] = {0x01, 0x02, 0x03, 0x04};
  uint32_t crc = crc32_ieee(data, sizeof(data));
  TEST_ASSERT_EQUAL_HEX32(TEST_CRC32_VECTOR_EXPECTED, crc);
}

// 3. Roundtrip Constructor -> Parser
static void test_builder_roundtrip() {
  FrameBuilder builder;
  FrameParser parser;

  const uint16_t command_id = TEST_CMD_ID;
  const uint8_t payload[] = {rpc::RPC_FRAME_DELIMITER, TEST_BYTE_01,
                             rpc::RPC_UINT8_MASK, TEST_BYTE_02,
                             rpc::RPC_FRAME_DELIMITER};

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(etl::span<uint8_t>(raw), command_id, 0,
                                 etl::span<const uint8_t>(payload, sizeof(payload)));

  TEST_ASSERT(raw_len > 0);
  TEST_ASSERT(raw[0] == PROTOCOL_VERSION);

  auto res = parser.parse(etl::span<const uint8_t>(raw, raw_len));
  TEST_ASSERT(res.has_value());
  TEST_ASSERT(res->header.command_id == command_id);
  TEST_ASSERT(res->payload.size() == sizeof(payload));
  TEST_ASSERT(memcmp(res->payload.data(), payload, sizeof(payload)) == 0);
}

static void test_builder_payload_limit() {
  FrameBuilder builder;
  uint8_t large_payload[MAX_PAYLOAD_SIZE + 1] = {0};
  uint8_t buffer[MAX_RAW_FRAME_SIZE];
  size_t len = builder.build(etl::span<uint8_t>(buffer), TEST_CMD_ID, 0,
                             etl::span<const uint8_t>(large_payload, sizeof(large_payload)));
  TEST_ASSERT(len == 0);
}

static void test_parser_incomplete_packets() {
  FrameParser parser;
  uint8_t short_packet[] = {PROTOCOL_VERSION, 0x00, 0x05};
  auto res = parser.parse(etl::span<const uint8_t>(short_packet, sizeof(short_packet)));
  TEST_ASSERT(!res.has_value());
  TEST_ASSERT(res.error() == FrameError::MALFORMED);
}

static void test_parser_crc_failure() {
  FrameBuilder builder;
  FrameParser parser;
  uint8_t raw[MAX_RAW_FRAME_SIZE];
  size_t raw_len = builder.build(etl::span<uint8_t>(raw), TEST_CMD_ID, 0, etl::span<const uint8_t>());
  raw[raw_len - 1] ^= 0xFF; // Corrupt CRC
  auto res = parser.parse(etl::span<const uint8_t>(raw, raw_len));
  TEST_ASSERT(!res.has_value());
  TEST_ASSERT(res.error() == FrameError::CRC_MISMATCH);
}

static void test_parser_header_validation() {
  FrameBuilder builder;
  FrameParser parser;
  uint8_t raw[MAX_RAW_FRAME_SIZE];
  size_t raw_len = builder.build(etl::span<uint8_t>(raw), TEST_CMD_ID, 0, etl::span<const uint8_t>());
  raw[0] = 0xFF; // Bad version
  auto res = parser.parse(etl::span<const uint8_t>(raw, raw_len));
  TEST_ASSERT(!res.has_value());
}

static void test_parser_overflow_guard() {
  FrameParser parser;
  uint8_t huge[MAX_RAW_FRAME_SIZE + 1];
  auto res = parser.parse(etl::span<const uint8_t>(huge, sizeof(huge)));
  TEST_ASSERT(!res.has_value());
}

static void test_parser_header_logical_validation_mismatch() {
  FrameBuilder builder;
  FrameParser parser;
  uint8_t raw[MAX_RAW_FRAME_SIZE];
  size_t raw_len = builder.build(etl::span<uint8_t>(raw), TEST_CMD_ID, 0, etl::span<const uint8_t>());
  // Sabotage length in header but keep physical size
  raw[2] = 0xFF; 
  auto res = parser.parse(etl::span<const uint8_t>(raw, raw_len));
  TEST_ASSERT(!res.has_value());
}

static void test_builder_buffer_too_small() {
  FrameBuilder builder;
  uint8_t small_buf[5];
  size_t len = builder.build(etl::span<uint8_t>(small_buf), TEST_CMD_ID, 0, etl::span<const uint8_t>());
  TEST_ASSERT(len == 0);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_endianness_helpers);
  RUN_TEST(test_crc_helpers);
  RUN_TEST(test_builder_roundtrip);
  RUN_TEST(test_builder_payload_limit);
  RUN_TEST(test_parser_incomplete_packets);
  RUN_TEST(test_parser_crc_failure);
  RUN_TEST(test_parser_header_validation);
  RUN_TEST(test_parser_overflow_guard);
  RUN_TEST(test_parser_header_logical_validation_mismatch);
  RUN_TEST(test_builder_buffer_too_small);
  return UNITY_END();
}
