#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "protocol/cobs.h"
#include "protocol/crc.h"
#include "protocol/rpc_frame.h"
#include "test_constants.h"
#include "test_support.h"

using namespace rpc;

static void test_endianness_helpers() {
  uint8_t buffer[2] = {
      static_cast<uint8_t>((TEST_CMD_ID >> 8) & rpc::RPC_UINT8_MASK),
      static_cast<uint8_t>(TEST_CMD_ID & rpc::RPC_UINT8_MASK),
  };
  TEST_ASSERT(read_u16_be(buffer) == TEST_CMD_ID);
  write_u16_be(buffer, TEST_WRITE_U16_VALUE);
  TEST_ASSERT(buffer[0] == ((TEST_WRITE_U16_VALUE >> 8) & rpc::RPC_UINT8_MASK) &&
              buffer[1] == (TEST_WRITE_U16_VALUE & rpc::RPC_UINT8_MASK));
}

static void test_crc_helpers() {
  const uint8_t data[] = {TEST_PAYLOAD_BYTE, TEST_BYTE_BB, TEST_BYTE_CC, TEST_BYTE_DD};
  uint32_t crc = crc32_ieee(data, sizeof(data));
  // Valor verificado con binascii.crc32 (polinomio IEEE 802.3).
  TEST_ASSERT(crc == TEST_CRC32_VECTOR_EXPECTED);
}

static void test_builder_roundtrip() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = TEST_CMD_ID;
  const uint8_t payload[] = {rpc::RPC_FRAME_DELIMITER, TEST_BYTE_01, rpc::RPC_UINT8_MASK, TEST_BYTE_02, rpc::RPC_FRAME_DELIMITER};

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
    size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));
    TEST_ASSERT(raw_len ==
                sizeof(FrameHeader) + sizeof(payload) + CRC_TRAILER_SIZE);

    uint32_t crc = read_u32_be(raw + raw_len - CRC_TRAILER_SIZE);
    TEST_ASSERT(crc == crc32_ieee(raw, raw_len - CRC_TRAILER_SIZE));

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  TEST_ASSERT(encoded_len > 0);

  bool parsed = false;
  for (size_t i = 0; i < encoded_len; ++i) {
    TEST_ASSERT(!parser.consume(encoded[i], frame));
  }
  parsed = parser.consume(rpc::RPC_FRAME_DELIMITER, frame);
  TEST_ASSERT(parsed);
  TEST_ASSERT(frame.header.version == PROTOCOL_VERSION);
  TEST_ASSERT(frame.header.command_id == command_id);
  TEST_ASSERT(frame.header.payload_length == sizeof(payload));
  TEST_ASSERT(test_memeq(frame.payload, payload, sizeof(payload)));
}

static void test_builder_payload_limit() {
  FrameBuilder builder;
  uint8_t payload[MAX_PAYLOAD_SIZE + 1];
  test_memfill(payload, sizeof(payload), TEST_BYTE_01);
  uint8_t buffer[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t len = builder.build(buffer, sizeof(buffer), TEST_CMD_ID, payload, sizeof(payload));
  TEST_ASSERT(len == 0);
}

static void test_parser_incomplete_packets() {
  FrameParser parser;
  Frame frame{};
  TEST_ASSERT(!parser.consume(TEST_BYTE_11, frame));
  TEST_ASSERT(!parser.consume(TEST_BYTE_22, frame));
  // Reset and provide terminating zero with no data -> should be ignored.
  TEST_ASSERT(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_crc_failure() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {TEST_BYTE_10, TEST_BYTE_20, TEST_BYTE_30};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_CRC_FAILURE, payload, sizeof(payload));
  TEST_ASSERT(raw_len > 0);

  raw[sizeof(FrameHeader)] ^= rpc::RPC_UINT8_MASK;  // Corrupt payload without fixing CRC.

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  for (size_t i = 0; i < encoded_len; ++i) {
    TEST_ASSERT(!parser.consume(encoded[i], frame));
  }
  TEST_ASSERT(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_header_validation() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {TEST_PAYLOAD_BYTE};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_HEADER_VALIDATION, payload, sizeof(payload));
  TEST_ASSERT(raw_len > 0);

  // Break protocol version.
  raw[0] = PROTOCOL_VERSION + 1;

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  for (size_t i = 0; i < encoded_len; ++i) {
    TEST_ASSERT(!parser.consume(encoded[i], frame));
  }
  TEST_ASSERT(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_overflow_guard() {
  FrameParser parser;
  Frame frame{};

  enum { kOverflowBufSize = rpc::MAX_RAW_FRAME_SIZE + 1024 };
  uint8_t encoded[kOverflowBufSize];
  size_t encoded_len = 0;

  size_t generated = 0;
  while (generated + 254 <= rpc::MAX_RAW_FRAME_SIZE) {
    TEST_ASSERT(encoded_len + 1 + 254 < kOverflowBufSize);
    encoded[encoded_len++] = rpc::RPC_UINT8_MASK;
    for (size_t i = 0; i < 254; ++i) {
      encoded[encoded_len++] = TEST_MARKER_BYTE;
    }
    generated += 254;
  }

  size_t remaining = rpc::MAX_RAW_FRAME_SIZE - generated;
  TEST_ASSERT(encoded_len + 1 + (remaining + 1) < kOverflowBufSize);
  encoded[encoded_len++] = static_cast<uint8_t>(remaining + 2);
  for (size_t i = 0; i < remaining + 1; ++i) {
    encoded[encoded_len++] = TEST_BYTE_33;
  }

  for (size_t i = 0; i < encoded_len; ++i) {
    TEST_ASSERT(!parser.consume(encoded[i], frame));
  }
  TEST_ASSERT(!parser.consume(rpc::RPC_FRAME_DELIMITER, frame));
}

static void test_parser_noise_handling() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = TEST_CMD_ID_NOISE;
  const uint8_t payload[] = {TEST_BYTE_DE, TEST_BYTE_AD, TEST_BYTE_BE, TEST_BYTE_EF};

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
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
  
  TEST_ASSERT(parsed);
  TEST_ASSERT(frame.header.command_id == command_id);
}

static void test_parser_fragmentation() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = TEST_CMD_ID_FRAGMENTATION;
  const uint8_t payload[] = {TEST_BYTE_01, TEST_BYTE_02, TEST_BYTE_03};

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);

  // Feed byte by byte with "delays" (logic check only)
  bool parsed = false;
  for (size_t i = 0; i < encoded_len; ++i) {
      parsed = parser.consume(encoded[i], frame);
      TEST_ASSERT(!parsed); // Should not be done until rpc::RPC_FRAME_DELIMITER
  }
  parsed = parser.consume(rpc::RPC_FRAME_DELIMITER, frame);
    TEST_ASSERT(parsed);
    TEST_ASSERT(frame.header.command_id == command_id);
}

// Test COBS encoding for ANALOG_READ_RESP with various values
// This is the specific case that was failing in production
static void test_analog_read_resp_encoding() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  // Test with multiple analog values that could trigger edge cases
  const uint16_t analog_values[] = {0, 1, 255, 256, 512, 1000, 1023};
  const uint16_t command_id = 86;  // CMD_ANALOG_READ_RESP = 0x56

  for (size_t v = 0; v < sizeof(analog_values) / sizeof(analog_values[0]); ++v) {
    parser.reset();

    uint16_t analog_value = analog_values[v];
    uint8_t payload[2];
    write_u16_be(payload, analog_value);

    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
    size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));
    TEST_ASSERT(raw_len > 0);

    // Verify raw frame structure
    // Header: version(1) + payload_len(2) + command_id(2) = 5 bytes
    // Payload: 2 bytes
    // CRC: 4 bytes
    // Total: 11 bytes
    TEST_ASSERT(raw_len == 11);

    uint8_t encoded[COBS_BUFFER_SIZE] = {0};
    size_t encoded_len = cobs::encode(raw, raw_len, encoded);
    TEST_ASSERT(encoded_len > 0);
    TEST_ASSERT(encoded_len <= raw_len + (raw_len / 254) + 1);

    // Verify no zero bytes in encoded data (COBS invariant)
    for (size_t i = 0; i < encoded_len; ++i) {
      TEST_ASSERT(encoded[i] != 0);
    }

    // Feed encoded frame to parser
    bool parsed = false;
    for (size_t i = 0; i < encoded_len; ++i) {
      parsed = parser.consume(encoded[i], frame);
      TEST_ASSERT(!parsed);  // Should not complete until delimiter
    }

    // Complete frame with delimiter
    parsed = parser.consume(rpc::RPC_FRAME_DELIMITER, frame);
    TEST_ASSERT(parsed);
    TEST_ASSERT(frame.header.command_id == command_id);
    TEST_ASSERT(frame.header.payload_length == 2);

    // Verify payload matches
    uint16_t decoded_value = read_u16_be(frame.payload);
    TEST_ASSERT(decoded_value == analog_value);
  }
}

// Test COBS encoding for DIGITAL_READ_RESP
static void test_digital_read_resp_encoding() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = 85;  // CMD_DIGITAL_READ_RESP = 0x55

  for (uint8_t digital_value = 0; digital_value <= 1; ++digital_value) {
    parser.reset();

    uint8_t payload[1] = {digital_value};

    uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
    size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));
    TEST_ASSERT(raw_len > 0);

    // Header: 5 bytes, Payload: 1 byte, CRC: 4 bytes = 10 bytes
    TEST_ASSERT(raw_len == 10);

    uint8_t encoded[COBS_BUFFER_SIZE] = {0};
    size_t encoded_len = cobs::encode(raw, raw_len, encoded);
    TEST_ASSERT(encoded_len > 0);

    // Verify no zero bytes in encoded data
    for (size_t i = 0; i < encoded_len; ++i) {
      TEST_ASSERT(encoded[i] != 0);
    }

    // Parse the frame
    bool parsed = false;
    for (size_t i = 0; i < encoded_len; ++i) {
      parsed = parser.consume(encoded[i], frame);
      TEST_ASSERT(!parsed);
    }

    parsed = parser.consume(rpc::RPC_FRAME_DELIMITER, frame);
    TEST_ASSERT(parsed);
    TEST_ASSERT(frame.header.command_id == command_id);
    TEST_ASSERT(frame.header.payload_length == 1);
    TEST_ASSERT(frame.payload[0] == digital_value);
  }
}

// Test native COBS encode/decode roundtrip
static void test_cobs_native_roundtrip() {
  // Test various patterns including zeros
  const uint8_t test_patterns[][8] = {
    {0x00},                                      // Single zero
    {0x00, 0x00},                                // Two zeros
    {0x01, 0x00, 0x02},                          // Zero in middle
    {0x00, 0x01, 0x02, 0x00},                    // Zeros at start and end
    {0x02, 0x00, 0x02, 0x00, 0x56},              // Header-like pattern
    {0x02, 0x00, 0x02, 0x00, 0x56, 0x03, 0xE8},  // Analog read resp pattern
    {0xFF, 0xFE, 0xFD, 0xFC, 0xFB, 0xFA, 0xF9, 0xF8},  // High bytes
  };
  const size_t pattern_lens[] = {1, 2, 3, 4, 5, 7, 8};

  for (size_t p = 0; p < sizeof(pattern_lens) / sizeof(pattern_lens[0]); ++p) {
    uint8_t encoded[16] = {0};
    uint8_t decoded[16] = {0};

    size_t enc_len = cobs::encode(test_patterns[p], pattern_lens[p], encoded);
    TEST_ASSERT(enc_len > 0);

    // Verify no zeros in encoded output
    for (size_t i = 0; i < enc_len; ++i) {
      TEST_ASSERT(encoded[i] != 0);
    }

    size_t dec_len = cobs::decode(encoded, enc_len, decoded);
    TEST_ASSERT(dec_len == pattern_lens[p]);

    // Verify decoded matches original
    for (size_t i = 0; i < pattern_lens[p]; ++i) {
      TEST_ASSERT(decoded[i] == test_patterns[p][i]);
    }
  }
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
  test_cobs_native_roundtrip();
  test_analog_read_resp_encoding();
  test_digital_read_resp_encoding();
  return 0;
}
