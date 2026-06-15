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

  // 3. serialize_frame error paths (buffer too small)
  etl::array<uint8_t, 2> small_buf;
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  TEST_ASSERT_EQUAL(
      0, serialize_frame(env, etl::span<uint8_t>(small_buf.data(), 2)));

  // 4. parse_frame error paths
  etl::array<uint8_t, 128> raw;
  raw.fill(0);

  // Malformed: too short
  TEST_ASSERT(
      !parse_frame(etl::span<const uint8_t>(raw.data(), 2)).has_value());

  // Malformed: wrong version
  rpc_pb_RpcEnvelope env_valid = rpc_pb_RpcEnvelope_init_default;
  env_valid.version = 0xFF;
  size_t v_len = serialize_frame(env_valid, raw);
  TEST_ASSERT(
      !parse_frame(etl::span<const uint8_t>(raw.data(), v_len)).has_value());

  // CRC Mismatch
  env_valid.version = PROTOCOL_VERSION;
  v_len = serialize_frame(env_valid, raw);
  TEST_ASSERT(v_len > 0);
  raw[v_len - 1] ^= 0xFF;  // Break CRC

  auto res = parse_frame(etl::span<const uint8_t>(raw.data(), v_len));
  TEST_ASSERT(!res.has_value());
  TEST_ASSERT(res.error() == FrameError::CRC_MISMATCH);

  // Correct CRC
  raw[v_len - 1] ^= 0xFF;  // Restore CRC
  TEST_ASSERT(
      parse_frame(etl::span<const uint8_t>(raw.data(), v_len)).has_value());
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
