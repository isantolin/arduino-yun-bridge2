#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

#include "protocol/cobs.h"
#include "protocol/crc.h"
#include "protocol/rpc_frame.h"
#include "test_constants.h"

using namespace rpc;

static void test_endianness_helpers() {
  uint8_t buffer[2] = {
      static_cast<uint8_t>((TEST_CMD_ID >> 8) & rpc::RPC_UINT8_MASK),
      static_cast<uint8_t>(TEST_CMD_ID & rpc::RPC_UINT8_MASK),
  };
  assert(read_u16_be(buffer) == TEST_CMD_ID);
  write_u16_be(buffer, TEST_WRITE_U16_VALUE);
  assert(buffer[0] == ((TEST_WRITE_U16_VALUE >> 8) & rpc::RPC_UINT8_MASK) &&
         buffer[1] == (TEST_WRITE_U16_VALUE & rpc::RPC_UINT8_MASK));
}

static void test_crc_helpers() {
  const uint8_t data[] = {TEST_PAYLOAD_BYTE, TEST_BYTE_BB, TEST_BYTE_CC, TEST_BYTE_DD};
  uint32_t crc = crc32_ieee(data, sizeof(data));
  // Valor verificado con binascii.crc32 (polinomio IEEE 802.3).
  assert(crc == TEST_CRC32_VECTOR_EXPECTED);
}

static void test_builder_roundtrip() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = TEST_CMD_ID;
  const uint8_t payload[] = {rpc::RPC_FRAME_DELIMITER, TEST_BYTE_01, rpc::RPC_UINT8_MASK, TEST_BYTE_02, rpc::RPC_FRAME_DELIMITER};

  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
    size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));
    assert(raw_len ==
      sizeof(FrameHeader) + sizeof(payload) + CRC_TRAILER_SIZE);

    uint32_t crc = read_u32_be(raw + raw_len - CRC_TRAILER_SIZE);
    assert(crc == crc32_ieee(raw, raw_len - CRC_TRAILER_SIZE));

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  assert(encoded_len > 0);

  bool parsed = false;
  for (size_t i = 0; i < encoded_len; ++i) {
    assert(!parser.consume(encoded[i], frame));
  }
  parsed = parser.consume(rpc::RPC_FRAME_DELIMITER, frame);
  assert(parsed);
  assert(frame.header.version == PROTOCOL_VERSION);
  assert(frame.header.command_id == command_id);
  assert(frame.header.payload_length == sizeof(payload));
  assert(std::memcmp(frame.payload, payload, sizeof(payload)) == 0);
}

static void test_builder_payload_limit() {
  FrameBuilder builder;
  std::vector<uint8_t> payload(MAX_PAYLOAD_SIZE + 1, TEST_BYTE_01);
  uint8_t buffer[MAX_RAW_FRAME_SIZE] = {0};
  size_t len = builder.build(buffer, sizeof(buffer), TEST_CMD_ID, payload.data(), payload.size());
  assert(len == 0);
}

static void test_parser_incomplete_packets() {
  FrameParser parser;
  Frame frame{};
  assert(!parser.consume(TEST_BYTE_11, frame));
  assert(!parser.consume(TEST_BYTE_22, frame));
  // Reset and provide terminating zero with no data -> should be ignored.
  assert(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_crc_failure() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {TEST_BYTE_10, TEST_BYTE_20, TEST_BYTE_30};
  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_CRC_FAILURE, payload, sizeof(payload));
  assert(raw_len > 0);

  raw[sizeof(FrameHeader)] ^= rpc::RPC_UINT8_MASK;  // Corrupt payload without fixing CRC.

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  for (size_t i = 0; i < encoded_len; ++i) {
    assert(!parser.consume(encoded[i], frame));
  }
  assert(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_header_validation() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {TEST_PAYLOAD_BYTE};
  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_HEADER_VALIDATION, payload, sizeof(payload));
  assert(raw_len > 0);

  // Break protocol version.
  raw[0] = PROTOCOL_VERSION + 1;

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  for (size_t i = 0; i < encoded_len; ++i) {
    assert(!parser.consume(encoded[i], frame));
  }
  assert(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_overflow_guard() {
  FrameParser parser;
  Frame frame{};

  std::vector<uint8_t> encoded;
  encoded.reserve(COBS_BUFFER_SIZE);

  size_t generated = 0;
  while (generated + 254 <= MAX_RAW_FRAME_SIZE) {
    encoded.push_back(rpc::RPC_UINT8_MASK);
    encoded.insert(encoded.end(), 254, TEST_MARKER_BYTE);
    generated += 254;
  }

  size_t remaining = MAX_RAW_FRAME_SIZE - generated;
  encoded.push_back(static_cast<uint8_t>(remaining + 2));
  encoded.insert(encoded.end(), remaining + 1, TEST_BYTE_33);

  for (uint8_t byte : encoded) {
    assert(!parser.consume(byte, frame));
  }
  assert(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_noise_handling() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = TEST_CMD_ID_NOISE;
  const uint8_t payload[] = {TEST_BYTE_DE, TEST_BYTE_AD, TEST_BYTE_BE, TEST_BYTE_EF};

  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);

  // Inject noise before the frame. 
  // Note: We must end with rpc::RPC_FRAME_DELIMITER to flush the noise as a "bad frame" 
  // so the parser is clean for the valid frame.
  const uint8_t noise[] = {TEST_BYTE_11, TEST_BYTE_22, rpc::RPC_FRAME_DELIMITER, TEST_BYTE_33, TEST_BYTE_44, rpc::RPC_FRAME_DELIMITER}; 
  for (uint8_t b : noise) {
    parser.consume(b, frame);
  }

  // Now feed the valid frame
  bool parsed = false;
  for (size_t i = 0; i < encoded_len; ++i) {
    if (parser.consume(encoded[i], frame)) {
        parsed = true;
    }
  }
  // The last byte (rpc::RPC_FRAME_DELIMITER) should trigger the parse
  parsed = parser.consume(rpc::RPC_FRAME_DELIMITER, frame);
  
  assert(parsed);
  assert(frame.header.command_id == command_id);
}

static void test_parser_fragmentation() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = TEST_CMD_ID_FRAGMENTATION;
  const uint8_t payload[] = {TEST_BYTE_01, TEST_BYTE_02, TEST_BYTE_03};

  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);

  // Feed byte by byte with "delays" (logic check only)
  bool parsed = false;
  for (size_t i = 0; i < encoded_len; ++i) {
      parsed = parser.consume(encoded[i], frame);
      assert(!parsed); // Should not be done until rpc::RPC_FRAME_DELIMITER
  }
  parsed = parser.consume(rpc::RPC_FRAME_DELIMITER, frame);
  assert(parsed);
  assert(frame.header.command_id == command_id);
}

int main() {
  test_endianness_helpers();
  test_crc_helpers();
  test_builder_roundtrip();
  test_builder_payload_limit();
  test_parser_incomplete_packets();
  test_parser_crc_failure();
  test_parser_overflow_guard();
  test_parser_header_validation();
  test_parser_noise_handling();
  test_parser_fragmentation();
  return 0;
}
