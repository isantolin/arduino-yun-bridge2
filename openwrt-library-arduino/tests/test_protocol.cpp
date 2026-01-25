#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <FastCRC.h>

#include "protocol/rpc_frame.h"
#include "test_constants.h"
#include "test_support.h"

using namespace rpc;

static FastCRC32 CRC32;

// 1. Helpers Básicos (Original)
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

// 2. Helpers CRC (Original)
static void test_crc_helpers() {
  const uint8_t data[] = {TEST_PAYLOAD_BYTE, TEST_BYTE_BB, TEST_BYTE_CC, TEST_BYTE_DD};
  uint32_t crc = CRC32.crc32(data, sizeof(data));
  TEST_ASSERT(crc == TEST_CRC32_VECTOR_EXPECTED);
}

// 3. Roundtrip Constructor -> Parser (Adaptado: Sin COBS)
static void test_builder_roundtrip() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint16_t command_id = TEST_CMD_ID;
  const uint8_t payload[] = {rpc::RPC_FRAME_DELIMITER, TEST_BYTE_01, rpc::RPC_UINT8_MASK, TEST_BYTE_02, rpc::RPC_FRAME_DELIMITER};

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));
  
  // Verificación de tamaño RAW (Header + Payload + CRC)
  TEST_ASSERT(raw_len == sizeof(FrameHeader) + sizeof(payload) + CRC_TRAILER_SIZE);

  uint32_t crc = read_u32_be(raw + raw_len - CRC_TRAILER_SIZE);
  TEST_ASSERT(crc == CRC32.crc32(raw, raw_len - CRC_TRAILER_SIZE));

  // [CAMBIO] En lugar de COBS encode -> decode, pasamos el buffer RAW directamente
  // simulando que PacketSerial ya hizo su trabajo.
  bool parsed = parser.parse(raw, raw_len, frame);
  
  TEST_ASSERT(parsed);
  TEST_ASSERT(frame.header.version == PROTOCOL_VERSION);
  TEST_ASSERT(frame.header.command_id == command_id);
  TEST_ASSERT(frame.header.payload_length == sizeof(payload));
  TEST_ASSERT(test_memeq(frame.payload, payload, sizeof(payload)));
}

// 4. Límite de Payload (Original)
static void test_builder_payload_limit() {
  FrameBuilder builder;
  uint8_t payload[MAX_PAYLOAD_SIZE + 1];
  test_memfill(payload, sizeof(payload), TEST_BYTE_01);
  uint8_t buffer[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t len = builder.build(buffer, sizeof(buffer), TEST_CMD_ID, payload, sizeof(payload));
  TEST_ASSERT(len == 0);
}

// 5. Paquetes Incompletos (Adaptado a API parse)
static void test_parser_incomplete_packets() {
  FrameParser parser;
  Frame frame{};
  
  uint8_t raw[10]; // Buffer dummy insuficiente para un frame real
  memset(raw, 0, sizeof(raw));

  // FrameParser.parse debe retornar false si el tamaño es menor al mínimo (Header + CRC)
  TEST_ASSERT(!parser.parse(raw, 4, frame)); // Menor que header
  TEST_ASSERT(!parser.parse(raw, sizeof(FrameHeader), frame)); // Header sin CRC
}

// 6. Fallo de CRC (Adaptado)
static void test_parser_crc_failure() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {TEST_BYTE_10, TEST_BYTE_20, TEST_BYTE_30};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_CRC_FAILURE, payload, sizeof(payload));
  TEST_ASSERT(raw_len > 0);

  raw[sizeof(FrameHeader)] ^= rpc::RPC_UINT8_MASK;  // Corromper payload

  TEST_ASSERT(!parser.parse(raw, raw_len, frame));
  TEST_ASSERT(parser.getError() == FrameParser::Error::CRC_MISMATCH);
}

// 7. Validación de Header (Versión) (Adaptado)
static void test_parser_header_validation() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};

  const uint8_t payload[] = {TEST_PAYLOAD_BYTE};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_HEADER_VALIDATION, payload, sizeof(payload));
  TEST_ASSERT(raw_len > 0);

  // Romper versión del protocolo
  raw[0] = PROTOCOL_VERSION + 1;
  
  // Recalcular CRC para que el fallo sea de Header y no de CRC
  uint32_t new_crc = CRC32.crc32(raw, raw_len - CRC_TRAILER_SIZE);
  write_u32_be(raw + raw_len - CRC_TRAILER_SIZE, new_crc);

  TEST_ASSERT(!parser.parse(raw, raw_len, frame));
  TEST_ASSERT(parser.getError() == FrameParser::Error::MALFORMED);
}

// 8. Buffer Overflow Guard (Adaptado)
static void test_parser_overflow_guard() {
  FrameParser parser;
  Frame frame{};

  uint8_t huge_buffer[rpc::MAX_RAW_FRAME_SIZE + 50];
  memset(huge_buffer, 0, sizeof(huge_buffer));

  // Intentar parsear un buffer que excede el máximo permitido por el protocolo
  TEST_ASSERT(!parser.parse(huge_buffer, sizeof(huge_buffer), frame));
  TEST_ASSERT(parser.getError() == FrameParser::Error::MALFORMED);
}

// 9. Lógica de Header inconsistente (Recuperado del original y adaptado)
static void test_parser_header_logical_validation_mismatch() {
  FrameBuilder builder;
  FrameParser parser;
  Frame frame{};
  
  uint8_t payload[] = {0x11, 0x22};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID, payload, sizeof(payload));
  
  // raw structure: [Ver][LenH][LenL][CmdH][CmdL][P1][P2][CRC]...
  // Payload real es 2 bytes. Cambiamos el header para decir que son 3.
  raw[2] = 3;
  
  // Recalcular CRC para pasar la primera validación
  uint32_t new_crc = CRC32.crc32(raw, raw_len - CRC_TRAILER_SIZE);
  write_u32_be(raw + raw_len - CRC_TRAILER_SIZE, new_crc);
  
  // Debe fallar porque el header dice length=3 pero el buffer raw solo tiene espacio para 2
  TEST_ASSERT(!parser.parse(raw, raw_len, frame));
  TEST_ASSERT(parser.getError() == FrameParser::Error::MALFORMED);
}

// 10. Buffer de Builder muy pequeño (Recuperado del original)
static void test_builder_buffer_too_small() {
  FrameBuilder builder;
  uint8_t payload[] = {0x11, 0x22};
  uint8_t small_buf[5]; // Muy pequeño para Header (5) + Payload (2) + CRC (4)
  
  size_t len = builder.build(small_buf, sizeof(small_buf), TEST_CMD_ID, payload, sizeof(payload));
  TEST_ASSERT(len == 0);
}

int main() {
  test_endianness_helpers();
  test_crc_helpers();
  test_builder_roundtrip();
  test_builder_payload_limit();
  test_parser_incomplete_packets();
  test_parser_crc_failure();
  test_parser_header_validation();
  test_parser_overflow_guard();
  
  // Tests de cobertura adicionales recuperados
  test_parser_header_logical_validation_mismatch();
  test_builder_buffer_too_small();
  
  return 0;
}
