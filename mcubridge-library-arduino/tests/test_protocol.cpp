#define BRIDGE_HOST_TEST 1
#include <Arduino.h>
#include <PigweedAVR.h>
#include <etl/array.h>
#include <etl/span.h>
#include <unity.h>

#include "protocol/mcu_wire.pb.h"
#include "protocol/rpc_protocol.h"
#include "test_support.h"

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

void setUp(void) {}
void tearDown(void) {}

void test_hdlc_roundtrip_and_crc() {
  etl::array<uint8_t, 256> out_buf;
  const uint8_t test_payload[] = {0x01, 0x02, 0x03, 0x04, 0x05};
  size_t encoded_len = TestHdlc::encodeUIFrame(
      1, etl::span<const uint8_t>(test_payload, sizeof(test_payload)), out_buf);
  TEST_ASSERT_TRUE(encoded_len > sizeof(test_payload));

  // Decode frame
  pw::hdlc::DecoderBuffer<256> decoder;
  bool frame_found = false;
  for (size_t i = 0; i < encoded_len; ++i) {
    auto res = decoder.Process(static_cast<std::byte>(out_buf[i]));
    if (res.ok()) {
      frame_found = true;
      TEST_ASSERT_EQUAL_UINT64(1, res.value().address());
      TEST_ASSERT_EQUAL(sizeof(test_payload), res.value().data().size());
      TEST_ASSERT_EQUAL_UINT8_ARRAY(test_payload, res.value().data().data(),
                                    sizeof(test_payload));
    }
  }
  TEST_ASSERT_TRUE(frame_found);

  // Test CRC corruption
  pw::hdlc::DecoderBuffer<256> corrupt_decoder;
  frame_found = false;
  // Corrupt payload byte
  out_buf[3] ^= 0xFF;
  for (size_t i = 0; i < encoded_len; ++i) {
    auto res = corrupt_decoder.Process(static_cast<std::byte>(out_buf[i]));
    if (res.ok()) {
      frame_found = true;
    }
  }
  TEST_ASSERT_FALSE(frame_found);
}

void test_hdlc_address_demux() {
  const uint64_t addresses[] = {1, 2, 3};

  for (uint64_t addr : addresses) {
    etl::array<uint8_t, 128> frame_buf;
    const uint8_t payload[] = {0xAA, 0xBB};
    size_t len = TestHdlc::encodeUIFrame(
        addr, etl::span<const uint8_t>(payload, sizeof(payload)), frame_buf);
    TEST_ASSERT_TRUE(len > 0);

    pw::hdlc::DecoderBuffer<256> decoder;
    bool found = false;
    for (size_t i = 0; i < len; ++i) {
      auto res = decoder.Process(static_cast<std::byte>(frame_buf[i]));
      if (res.ok()) {
        found = true;
        TEST_ASSERT_EQUAL_UINT64(addr, res.value().address());
      }
    }
    TEST_ASSERT_TRUE(found);
  }
}

void test_rpc_packet_encoding_and_decoding() {
  pw::rpc_avr::RpcPacket packet;
  packet.type = pw::rpc_avr::PacketType::kRequest;
  packet.channel_id = 1;
  packet.service_id = pw::tokenizer::Hash("bridge.SystemService");
  packet.method_id = pw::tokenizer::Hash("GetVersion");
  packet.call_id = 42;

  const uint8_t payload[] = {0x10, 0x20, 0x30};
  packet.payload = pw::ConstByteSpan(
      reinterpret_cast<const std::byte*>(payload), sizeof(payload));

  uint8_t encoded_buf[128];
  auto enc_res = packet.Encode(pw::as_writable_bytes(pw::span(encoded_buf)));
  TEST_ASSERT_TRUE(enc_res.ok());
  TEST_ASSERT_TRUE(enc_res.size() > 0);

  auto dec_res = pw::rpc_avr::RpcPacket::Decode(
      pw::as_bytes(pw::span(encoded_buf, enc_res.size())));
  TEST_ASSERT_TRUE(dec_res.ok());

  const auto& decoded = dec_res.value();
  TEST_ASSERT_TRUE(decoded.type == pw::rpc_avr::PacketType::kRequest);
  TEST_ASSERT_EQUAL_UINT32(1, decoded.channel_id);
  TEST_ASSERT_EQUAL_UINT32(packet.service_id, decoded.service_id);
  TEST_ASSERT_EQUAL_UINT32(packet.method_id, decoded.method_id);
  TEST_ASSERT_EQUAL_UINT32(42, decoded.call_id);
  TEST_ASSERT_EQUAL(sizeof(payload), decoded.payload.size());
  TEST_ASSERT_EQUAL_UINT8_ARRAY(payload, decoded.payload.data(),
                                sizeof(payload));
}

void test_service_id_tokenizer_hashing() {
  constexpr uint32_t kSystem = pw::tokenizer::Hash("bridge.SystemService");
  constexpr uint32_t kGpio = pw::tokenizer::Hash("bridge.GpioService");
  constexpr uint32_t kDataStore =
      pw::tokenizer::Hash("bridge.DataStoreService");
  constexpr uint32_t kMailbox = pw::tokenizer::Hash("bridge.MailboxService");
  constexpr uint32_t kProcess = pw::tokenizer::Hash("bridge.ProcessService");
  constexpr uint32_t kFileSystem =
      pw::tokenizer::Hash("bridge.FileSystemService");

  TEST_ASSERT_NOT_EQUAL(0, kSystem);
  TEST_ASSERT_NOT_EQUAL(0, kGpio);
  TEST_ASSERT_NOT_EQUAL(kSystem, kGpio);
  TEST_ASSERT_NOT_EQUAL(kDataStore, kMailbox);
  TEST_ASSERT_NOT_EQUAL(kProcess, kFileSystem);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_hdlc_roundtrip_and_crc);
  RUN_TEST(test_hdlc_address_demux);
  RUN_TEST(test_rpc_packet_encoding_and_decoding);
  RUN_TEST(test_service_id_tokenizer_hashing);
  return UNITY_END();
}
