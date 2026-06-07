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

  // 2. Custom CRC32 policy validation
  CRC32 crc_policy;
  crc_policy.reset();
  crc_policy.add(1);
  crc_policy.add(2);
  crc_policy.add(3);
  uint32_t expected_crc = etl::crc32(etl::array<uint8_t, 3>{1, 2, 3}.begin(), etl::array<uint8_t, 3>{1, 2, 3}.end());
  TEST_ASSERT_EQUAL_UINT32(expected_crc, crc_policy.value());

  // 3. serialize_frame error paths (buffer too small)
  etl::array<uint8_t, 2> small_buf;
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  TEST_ASSERT_EQUAL(
      0, serialize_frame(env, etl::span<uint8_t>(small_buf.data(), 2)));

  // 4. parse_frame paths
  etl::array<uint8_t, 128> raw;
  raw.fill(0);

  // Malformed: too short
  TEST_ASSERT(!parse_frame(etl::span<const uint8_t>(raw.data(), 2)).has_value());

  // Malformed: wrong version
  rpc_pb_RpcEnvelope env_valid = rpc_pb_RpcEnvelope_init_default;
  env_valid.version = 0xFF;
  size_t v_len = serialize_frame(env_valid, raw);
  TEST_ASSERT(v_len > 0);
  TEST_ASSERT(!parse_frame(etl::span<const uint8_t>(raw.data(), v_len)).has_value());

  // Successful roundtrip
  env_valid.version = PROTOCOL_VERSION;
  v_len = serialize_frame(env_valid, raw);
  TEST_ASSERT(v_len > 0);
  TEST_ASSERT(parse_frame(etl::span<const uint8_t>(raw.data(), v_len)).has_value());
}

void test_protocol_builder_exhaustive() {
  using namespace rpc;
  etl::array<uint8_t, 128> buf;
  etl::array<uint8_t, 3> payload = {1, 2, 3};
  
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = PROTOCOL_VERSION;
  env.command_id = (uint16_t)CommandId::CMD_GET_VERSION;
  env.sequence_id = 1;
  env.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_tag;
  etl::copy_n(payload.begin(), 3, env.payload_type.encrypted_payload.bytes);
  env.payload_type.encrypted_payload.size = 3;

  // Success path
  size_t len = serialize_frame(env, etl::span<uint8_t>(buf.data(), 128));
  TEST_ASSERT(len > 0);

  // Buffer too small
  len = serialize_frame(env, etl::span<uint8_t>(buf.data(), 2));
  TEST_ASSERT_EQUAL(0, len);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_protocol_frame_logic_exhaustive);
  RUN_TEST(test_protocol_builder_exhaustive);
  return UNITY_END();
}
