
#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif
#include <Arduino.h>
#include <string.h>
#if defined(ARDUINO_ARCH_AVR) || defined(__AVR__)
#include <PigweedAVR.h>
#else
#include <pw_hdlc/decoder.h>
#include <pw_hdlc/encoder.h>
#include <pw_status/status.h>
#include <pw_tokenizer/hash.h>
#endif
#include <etl/array.h>
#include <pw_span/span.h>
#include "pw_unit_test/framework.h"

#include "test_support.h"

HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

TEST(Protocol, HdlcRoundtripAndCrc) {
  etl::array<uint8_t, 256> out_buf;
  const uint8_t test_payload[] = {0x01, 0x02, 0x03, 0x04, 0x05};
  size_t encoded_len = TestHdlc::encodeUIFrame(
      1, pw::span<const uint8_t>(test_payload, sizeof(test_payload)), out_buf);
  EXPECT_TRUE(encoded_len > sizeof(test_payload));

  // Decode frame
  pw::hdlc::DecoderBuffer<256> decoder;
  bool frame_found = false;
  for (size_t i = 0; i < encoded_len; ++i) {
    auto res =
        decoder.Process(static_cast<pw::ByteSpan::element_type>(out_buf[i]));
    if (res.ok()) {
      frame_found = true;
      EXPECT_EQ(1, res.value().address());
      EXPECT_EQ(sizeof(test_payload), res.value().data().size());
      EXPECT_EQ(0, memcmp(test_payload, res.value().data().data(), sizeof(test_payload)));
    }
  }
  EXPECT_TRUE(frame_found);

  // Test CRC corruption
  pw::hdlc::DecoderBuffer<256> corrupt_decoder;
  frame_found = false;
  // Corrupt payload byte
  out_buf[3] ^= 0xFF;
  for (size_t i = 0; i < encoded_len; ++i) {
    auto res = corrupt_decoder.Process(
        static_cast<pw::ByteSpan::element_type>(out_buf[i]));
    if (res.ok()) {
      frame_found = true;
    }
  }
  EXPECT_FALSE(frame_found);
}

TEST(Protocol, HdlcAddressDemux) {
  const uint64_t addresses[] = {1, 2, 3};

  for (uint64_t addr : addresses) {
    etl::array<uint8_t, 128> frame_buf;
    const uint8_t payload[] = {0xAA, 0xBB};
    size_t len = TestHdlc::encodeUIFrame(
        addr, pw::span<const uint8_t>(payload, sizeof(payload)), frame_buf);
    EXPECT_TRUE(len > 0);

    pw::hdlc::DecoderBuffer<256> decoder;
    bool found = false;
    for (size_t i = 0; i < len; ++i) {
      auto res = decoder.Process(
          static_cast<pw::ByteSpan::element_type>(frame_buf[i]));
      if (res.ok()) {
        found = true;
        EXPECT_EQ(addr, res.value().address());
      }
    }
    EXPECT_TRUE(found);
  }
}

TEST(Protocol, RpcPacketEncodingAndDecoding) {
  const uint8_t payload[] = {0x10, 0x20, 0x30};
  pw::rpc::internal::Packet packet(
      pw::rpc::internal::pwpb::PacketType::REQUEST, 1,
      pw::tokenizer::Hash("rpc.pb.SystemService"),
      pw::tokenizer::Hash("GetVersion"), 42,
      pw::as_bytes(pw::span(payload, sizeof(payload))));

  uint8_t encoded_buf[128];
  auto enc_res = packet.Encode(pw::as_writable_bytes(pw::span(encoded_buf)));
  EXPECT_TRUE(enc_res.ok());
  EXPECT_TRUE(enc_res.value().size() > 0);

  auto dec_res = pw::rpc::internal::Packet::FromBuffer(enc_res.value());
  EXPECT_TRUE(dec_res.ok());

  const auto& decoded = dec_res.value();
  EXPECT_TRUE(decoded.type() ==
                   pw::rpc::internal::pwpb::PacketType::REQUEST);
  EXPECT_EQ(1, decoded.channel_id());
  EXPECT_EQ(packet.service_id(), decoded.service_id());
  EXPECT_EQ(packet.method_id(), decoded.method_id());
  EXPECT_EQ(42, decoded.call_id());
  EXPECT_EQ(sizeof(payload), decoded.payload().size());
  EXPECT_EQ(0, memcmp(payload, decoded.payload().data(), sizeof(payload)));
}

TEST(Protocol, ServiceIdTokenizerHashing) {
  constexpr uint32_t kSystem = pw::tokenizer::Hash("rpc.pb.SystemService");
  constexpr uint32_t kGpio = pw::tokenizer::Hash("rpc.pb.GpioService");
  constexpr uint32_t kDataStore =
      pw::tokenizer::Hash("rpc.pb.DataStoreService");
  constexpr uint32_t kMailbox = pw::tokenizer::Hash("rpc.pb.MailboxService");
  constexpr uint32_t kProcess = pw::tokenizer::Hash("rpc.pb.ProcessService");
  constexpr uint32_t kFileSystem =
      pw::tokenizer::Hash("rpc.pb.FileSystemService");

  EXPECT_NE(0, kSystem);
  EXPECT_NE(0, kGpio);
  EXPECT_NE(kSystem, kGpio);
  EXPECT_NE(kDataStore, kMailbox);
  EXPECT_NE(kProcess, kFileSystem);
}

