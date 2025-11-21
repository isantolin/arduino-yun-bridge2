#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

#include "protocol/cobs.h"
#include "protocol/crc.h"
#include "protocol/rpc_frame.h"

using namespace rpc;

static void test_endianness_helpers() {
  uint8_t buffer[2] = {0x12, 0x34};
  assert(read_u16_be(buffer) == 0x1234);
  write_u16_be(buffer, 0xCDEF);
  assert(buffer[0] == 0xCD && buffer[1] == 0xEF);
}

static void test_crc_helpers() {
  const uint8_t data[] = {0xAA, 0xBB, 0xCC, 0xDD};
  uint16_t crc = crc16_ccitt(data, sizeof(data));
  // Valor calculado con crc_hqx (polinomio 0x1021, seed 0xFFFF).
  assert(crc == 0x41FA);

  uint16_t incremental = crc16_ccitt_init();
  for (uint8_t byte : data) {
    incremental = crc16_ccitt_update(incremental, byte);
  }
  assert(incremental == crc);
}

static void test_builder_roundtrip() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = 0x42AB;
  const uint8_t payload[] = {0x00, 0x01, 0xFF, 0x02, 0x00};

  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, command_id, payload, sizeof(payload));
  assert(raw_len == sizeof(FrameHeader) + sizeof(payload) + sizeof(uint16_t));

  uint16_t crc = read_u16_be(raw + raw_len - sizeof(uint16_t));
  assert(crc == crc16_ccitt(raw, raw_len - sizeof(uint16_t)));

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  assert(encoded_len > 0);

  bool parsed = false;
  for (size_t i = 0; i < encoded_len; ++i) {
    assert(!parser.consume(encoded[i], frame));
  }
  parsed = parser.consume(0x00, frame);
  assert(parsed);
  assert(frame.header.version == PROTOCOL_VERSION);
  assert(frame.header.command_id == command_id);
  assert(frame.header.payload_length == sizeof(payload));
  assert(std::memcmp(frame.payload, payload, sizeof(payload)) == 0);
}

static void test_builder_payload_limit() {
  FrameBuilder builder;
  std::vector<uint8_t> payload(MAX_PAYLOAD_SIZE + 1, 0x01);
  uint8_t buffer[MAX_RAW_FRAME_SIZE] = {0};
  size_t len = builder.build(buffer, 0x1234, payload.data(), payload.size());
  assert(len == 0);
}

static void test_parser_incomplete_packets() {
  FrameParser parser;
  Frame frame{};
  assert(!parser.consume(0x11, frame));
  assert(!parser.consume(0x22, frame));
  // Reset and provide terminating zero with no data -> should be ignored.
  assert(!parser.consume(0x00, frame));
}

static void test_parser_crc_failure() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {0x10, 0x20, 0x30};
  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, 0x1111, payload, sizeof(payload));
  assert(raw_len > 0);

  raw[sizeof(FrameHeader)] ^= 0xFF;  // Corrupt payload without fixing CRC.

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  for (size_t i = 0; i < encoded_len; ++i) {
    assert(!parser.consume(encoded[i], frame));
  }
  assert(!parser.consume(0x00, frame));
}

static void test_parser_header_validation() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {0xAA};
  uint8_t raw[MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, 0x0102, payload, sizeof(payload));
  assert(raw_len > 0);

  // Break protocol version.
  raw[0] = PROTOCOL_VERSION + 1;

  uint8_t encoded[COBS_BUFFER_SIZE] = {0};
  size_t encoded_len = cobs::encode(raw, raw_len, encoded);
  for (size_t i = 0; i < encoded_len; ++i) {
    assert(!parser.consume(encoded[i], frame));
  }
  assert(!parser.consume(0x00, frame));
}

static void test_parser_overflow_guard() {
  FrameParser parser;
  Frame frame{};

  std::vector<uint8_t> encoded;
  encoded.reserve(COBS_BUFFER_SIZE);

  size_t generated = 0;
  while (generated + 254 <= MAX_RAW_FRAME_SIZE) {
    encoded.push_back(0xFF);
    encoded.insert(encoded.end(), 254, 0x55);
    generated += 254;
  }

  size_t remaining = MAX_RAW_FRAME_SIZE - generated;
  encoded.push_back(static_cast<uint8_t>(remaining + 2));
  encoded.insert(encoded.end(), remaining + 1, 0x33);

  for (uint8_t byte : encoded) {
    assert(!parser.consume(byte, frame));
  }
  assert(!parser.consume(0x00, frame));
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
  return 0;
}
