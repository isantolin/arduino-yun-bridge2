#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "Bridge.h"
#include "protocol/rpc_frame.h"
#include "test_constants.h"
#include "test_support.h"

using namespace rpc;

HardwareSerial Serial;
HardwareSerial Serial1;
BridgeClass Bridge(Serial1);
ConsoleClass Console;
DataStoreClass DataStore;
MailboxClass Mailbox;
FileSystemClass FileSystem;
ProcessClass Process;

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
  uint32_t crc = crc32_ieee(data, sizeof(data));
  TEST_ASSERT(crc == TEST_CRC32_VECTOR_EXPECTED);
}

// 3. Roundtrip Constructor -> Parser (Adaptado: Sin COBS)
static void test_builder_roundtrip() {
  FrameBuilder builder;
  FrameParser parser;

  const uint16_t command_id = TEST_CMD_ID;
  const uint8_t payload[] = {rpc::RPC_FRAME_DELIMITER, TEST_BYTE_01, rpc::RPC_UINT8_MASK, TEST_BYTE_02, rpc::RPC_FRAME_DELIMITER};

  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), command_id, payload, sizeof(payload));
  
  // Verificación de tamaño RAW (Header + Payload + CRC)
  TEST_ASSERT(raw_len == sizeof(FrameHeader) + sizeof(payload) + CRC_TRAILER_SIZE);

  uint32_t crc = read_u32_be(raw + raw_len - CRC_TRAILER_SIZE);
  TEST_ASSERT(crc == crc32_ieee(raw, raw_len - CRC_TRAILER_SIZE));

  // [SIL-2] New etl::expected API
  auto result = parser.parse(raw, raw_len);
  
  TEST_ASSERT(result.has_value());
  Frame frame = result.value();
  TEST_ASSERT(frame.header.version == PROTOCOL_VERSION);
  TEST_ASSERT(frame.header.command_id == command_id);
  TEST_ASSERT(frame.header.payload_length == sizeof(payload));
  TEST_ASSERT(test_memeq(frame.payload.data(), payload, sizeof(payload)));
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
  
  uint8_t raw[10]; // Buffer dummy insuficiente para un frame real
  etl::fill_n(raw, sizeof(raw), uint8_t{0});

  // [SIL-2] etl::expected API - parse returns error for incomplete packets
  auto result1 = parser.parse(raw, 4); // Menor que header
  TEST_ASSERT(!result1.has_value());
  
  auto result2 = parser.parse(raw, sizeof(FrameHeader)); // Header sin CRC
  TEST_ASSERT(!result2.has_value());
}

// 6. Fallo de CRC (Adaptado)
static void test_parser_crc_failure() {
  FrameBuilder builder;
  FrameParser parser;

  const uint8_t payload[] = {TEST_BYTE_10, TEST_BYTE_20, TEST_BYTE_30};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_CRC_FAILURE, payload, sizeof(payload));
  TEST_ASSERT(raw_len > 0);

  raw[sizeof(FrameHeader)] ^= rpc::RPC_UINT8_MASK;  // Corromper payload

  // [SIL-2] etl::expected API
  auto result = parser.parse(raw, raw_len);
  TEST_ASSERT(!result.has_value());
  TEST_ASSERT(result.error() == FrameError::CRC_MISMATCH);
}

// 7. Validación de Header (Versión) (Adaptado)
static void test_parser_header_validation() {
  FrameBuilder builder;
  FrameParser parser;

  const uint8_t payload[] = {TEST_PAYLOAD_BYTE};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE] = {0};
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID_HEADER_VALIDATION, payload, sizeof(payload));
  TEST_ASSERT(raw_len > 0);

  // Romper versión del protocolo
  raw[0] = PROTOCOL_VERSION + 1;
  
  // Recalcular CRC para que el fallo sea de Header y no de CRC
  uint32_t new_crc = crc32_ieee(raw, raw_len - CRC_TRAILER_SIZE);
  write_u32_be(raw + raw_len - CRC_TRAILER_SIZE, new_crc);

  // [SIL-2] etl::expected API
  auto result = parser.parse(raw, raw_len);
  TEST_ASSERT(!result.has_value());
  TEST_ASSERT(result.error() == FrameError::MALFORMED);
}

// 8. Buffer Overflow Guard (Adaptado)
static void test_parser_overflow_guard() {
  FrameParser parser;

  uint8_t huge_buffer[rpc::MAX_RAW_FRAME_SIZE + 50];
  etl::fill_n(huge_buffer, sizeof(huge_buffer), uint8_t{0});

  // [SIL-2] etl::expected API - Intentar parsear un buffer que excede el máximo
  auto result = parser.parse(huge_buffer, sizeof(huge_buffer));
  TEST_ASSERT(!result.has_value());
  TEST_ASSERT(result.error() == FrameError::MALFORMED);
}

// 9. Lógica de Header inconsistente (Recuperado del original y adaptado)
static void test_parser_header_logical_validation_mismatch() {
  FrameBuilder builder;
  FrameParser parser;
  
  uint8_t payload[] = {0x11, 0x22};
  uint8_t raw[rpc::MAX_RAW_FRAME_SIZE];
  size_t raw_len = builder.build(raw, sizeof(raw), TEST_CMD_ID, payload, sizeof(payload));
  
  // raw structure: [Ver][LenH][LenL][CmdH][CmdL][P1][P2][CRC]...
  // Payload real es 2 bytes. Cambiamos el header para decir que son 3.
  raw[2] = 3;
  
  // Recalcular CRC para pasar la primera validación
  uint32_t new_crc = crc32_ieee(raw, raw_len - CRC_TRAILER_SIZE);
  write_u32_be(raw + raw_len - CRC_TRAILER_SIZE, new_crc);
  
  // [SIL-2] etl::expected API - debe fallar porque header dice length=3 pero buffer tiene 2
  auto result = parser.parse(raw, raw_len);
  TEST_ASSERT(!result.has_value());
  TEST_ASSERT(result.error() == FrameError::MALFORMED);
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
