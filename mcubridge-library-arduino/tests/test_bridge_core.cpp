#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <Arduino.h>
#include <string.h>
#include <BridgeFaultInjection.h>
#include <etl/array.h>
#include <etl/numeric.h>
#include <etl/string_view.h>
#include <pw_bytes/endian.h>
#include <pw_metric_proto/metric_service.pwpb.h>
#include <pw_protobuf/decoder.h>
#include <pw_span/span.h>
#include "pw_unit_test/framework.h"

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "security/security.h"
#include "services/Console.h"
#include "services/SPIService.h"
#include "test_support.h"

using namespace bridge::test;

class FlowControlStream final : public Stream {
 public:
  int available() override {
    const int result =
        _available_calls++ == 0
            ? 0
            : static_cast<int>(bridge::config::FLOW_CONTROL_XON_THRESHOLD);
    return result;
  }
  int read() override { return -1; }
  int peek() override { return -1; }
  size_t write(uint8_t) override { return 1U; }
  size_t write(const uint8_t*, size_t size) override { return size; }
  void flush() override {}

 private:
  int _available_calls = 0;
};

class BridgeCoreFixture : public testing::Test {
 protected:
  void TearDown() override { Bridge.setStream(Serial); }
};

void reset_bridge(const char* secret = nullptr) {
  Bridge.begin(115200, secret);
  Console.begin();
}

TEST_F(BridgeCoreFixture, BridgeInitialization) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  EXPECT_FALSE(ba.isSynchronized());
}

TEST_F(BridgeCoreFixture, BridgeHandshake) {
  reset_bridge("6368616e67656d65313233");
  auto& ba = TestAccessor::create(Bridge);
  EXPECT_FALSE(ba.isSynchronized());

  EXPECT_TRUE(perform_test_link_sync(ba, 0x42));
  Bridge.process();

  EXPECT_TRUE(ba.isSynchronized());
}

TEST_F(BridgeCoreFixture, BridgeHandshakeResponseUsesTagLength) {
  BiStream stream;
  stream.clear();
  constexpr char kSharedSecret[] = "top-secret";
  reset_bridge_core(Bridge, stream, 0U, kSharedSecret);
  stream.tx_buf.clear();
  auto& ba = TestAccessor::create(Bridge);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> nonce = {};
  nonce.fill(0x42U);
  const auto secret =
      pw::span<const uint8_t>(reinterpret_cast<const uint8_t*>(kSharedSecret),
                              sizeof(kSharedSecret) - 1U);
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> request_tag = {};
  EXPECT_TRUE(rpc::security::handshake_generate_tag(secret, nonce, request_tag));

  rpc_pb_LinkSync request = rpc_pb_LinkSync_init_default;
  etl::copy_n(nonce.begin(), nonce.size(), request.nonce.bytes);
  request.nonce.size = nonce.size();
  etl::copy_n(request_tag.begin(), request_tag.size(), request.tag.bytes);
  request.tag.size = request_tag.size();

  EXPECT_GT(send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                          pw::tokenizer::Hash("LinkSync"),
                          rpc_pb_LinkSync_fields, request), 0);

  rpc_pb_LinkSync response = rpc_pb_LinkSync_init_default;
  EXPECT_TRUE(decode_tx_response(stream, rpc_pb_LinkSync_fields, response));
  EXPECT_EQ(rpc::RPC_HANDSHAKE_TAG_LENGTH, response.tag.size);
  // Handshake v2: the MCU returns its own nonce and the tag covers both.
  EXPECT_EQ(rpc::RPC_HANDSHAKE_NONCE_LENGTH, response.mcu_nonce.size);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> expected_response_tag = {};
  EXPECT_TRUE(rpc::security::handshake_generate_response_tag(
      secret, nonce,
      pw::span<const uint8_t>(response.mcu_nonce.bytes,
                              response.mcu_nonce.size),
      expected_response_tag));
  EXPECT_EQ(0, memcmp(expected_response_tag.data(), response.tag.bytes, expected_response_tag.size())));
}

TEST_F(BridgeCoreFixture, BridgeSendFrame) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> payload = {0xAA, 0xBB};
  EXPECT_TRUE(Bridge.sendConsoleStream(pw::span<const uint8_t>(payload.data(), 2)));
  EXPECT_TRUE(Bridge.sendLogStream(pw::span<const uint8_t>(payload.data(), 2)));
  pw_log_avr_HandleLog(1, payload.data(), payload.size());

  rpc_pb_PinSubscribeRequest sub_req = rpc_pb_PinSubscribeRequest_init_default;
  sub_req.pin = 13;
  sub_req.enabled = true;
  EXPECT_GT(send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                          pw::tokenizer::Hash("SubscribePins"),
                          rpc_pb_PinSubscribeRequest_fields, sub_req), 0);

  EXPECT_TRUE(Bridge.sendPinEvent(13, 1));
}

TEST_F(BridgeCoreFixture, BridgeProcessRx) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_DigitalWrite msg = rpc_pb_DigitalWrite_init_default;
  msg.pin = 13;
  msg.value = 1;
  EXPECT_GT(send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                          pw::tokenizer::Hash("DigitalWrite"),
                          rpc_pb_DigitalWrite_fields, msg), 0);
  Bridge.process();

  EXPECT_EQ(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  EXPECT_EQ(HIGH, g_arduino_stub_pin_states[13U].value);
}

TEST_F(BridgeCoreFixture, BridgeDedupConsoleWrite) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Console raw stream arrives on HDLC Address 0x03
  const char* text = "TEST";
  etl::array<uint8_t, 64> hdlc_buf;
  size_t len = TestHdlc::encodeUIFrame(
      3, pw::span<const uint8_t>(reinterpret_cast<const uint8_t*>(text), 4),
      hdlc_buf);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), len));
  EXPECT_EQ(4, Console.available());
}

TEST_F(BridgeCoreFixture, BridgeStatusAck) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Verify RpcPacket response routing
  pw::rpc::internal::Packet pkt(pw::rpc::internal::pwpb::PacketType::RESPONSE,
                                1, pw::tokenizer::Hash("rpc.pb.SystemService"),
                                pw::tokenizer::Hash("GetVersion"), 0,
                                pw::ConstByteSpan());
  ba.invokeRpcResponse(pkt);
  EXPECT_TRUE(ba.isSynchronized());
}

TEST_F(BridgeCoreFixture, BridgePostAndStackSentinel) {
  EXPECT_TRUE(bridge::hal::run_power_on_self_tests());
  EXPECT_TRUE(bridge::hal::checkStackOverflow());
  EXPECT_GE(bridge::hal::getFreeMemory(), bridge::hal::MIN_STACK_MARGIN_BYTES);
  EXPECT_TRUE(Bridge.isPostPassed());
}

TEST_F(BridgeCoreFixture, BridgeAwaitSynchronizationBounded) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);

  const uint32_t start_ms = bridge::test::fault::clock_ms();
  EXPECT_FALSE(Bridge.awaitSynchronization(50U, 5U));
  EXPECT_GE(bridge::test::fault::clock_ms() - start_ms, 50U);
  EXPECT_FALSE(Bridge.isSynchronized());
  EXPECT_FALSE(ba.isTxEnabled());

  EXPECT_FALSE(Bridge.awaitSynchronization(3U, 0U));

  reset_bridge();
  ba.setSynchronized();
  const uint32_t sync_start_ms = bridge::test::fault::clock_ms();
  EXPECT_TRUE(Bridge.awaitSynchronization(50U, 5U));
  EXPECT_EQ(sync_start_ms, bridge::test::fault::clock_ms());
  EXPECT_TRUE(ba.isTxEnabled());
}

TEST_F(BridgeCoreFixture, BridgeWcetTracking) {
  Bridge.resetWcetStats();
  EXPECT_EQ(0, Bridge.getWcetMaxMicros());
  Bridge.process();
  EXPECT_GE(Bridge.getWcetMaxMicros(), 0);
}

TEST_F(BridgeCoreFixture, BridgeBeginNullSecret) {
  Bridge.begin(115200, nullptr);
  EXPECT_TRUE(Bridge.isPostPassed());
  Bridge.process();
}

TEST_F(BridgeCoreFixture, BridgeHandshakeTimeoutNotSync) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  EXPECT_FALSE(ba.isSynchronized());

  ba.onHandshakeTimeout();
  EXPECT_TRUE(ba.isFault());

  reset_bridge();
  ba.setSynchronized();
  ba.onHandshakeTimeout();
  EXPECT_FALSE(ba.isFault());
}

namespace {
pw::Status g_last_status = pw::OkStatus();
size_t g_last_status_len = 0U;
size_t g_status_calls = 0U;
void capture_status_handler(pw::Status s, pw::span<const uint8_t> p) {
  g_last_status = s;
  g_last_status_len = p.size();
  ++g_status_calls;
}
}  // namespace

TEST_F(BridgeCoreFixture, BridgeQueueFullAndFlowControl) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 10> cdata{};
  cdata.fill('c');
  EXPECT_TRUE(Bridge.sendConsoleStream(cdata));
  EXPECT_TRUE(Bridge.isSynchronized());
}

TEST_F(BridgeCoreFixture, BridgeEmitStatusVariants) {
  reset_bridge();
  g_status_calls = 0U;
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_status_handler>());

  Bridge.emitStatus(pw::OkStatus());
  EXPECT_EQ(1U, g_status_calls);
  EXPECT_EQ(static_cast<int>(pw::OkStatus().code()), static_cast<int>(g_last_status.code()));
  EXPECT_EQ(0U, g_last_status_len);

  Bridge.emitStatus(pw::Status::Internal(), etl::string_view("Error"));
  EXPECT_EQ(2U, g_status_calls);
  EXPECT_EQ(static_cast<int>(pw::Status::Internal().code()), static_cast<int>(g_last_status.code()));
  EXPECT_EQ(5U, g_last_status_len);

  const etl::array<uint8_t, 3> raw = {1U, 2U, 3U};
  Bridge.emitStatus(pw::Status::InvalidArgument(),
                    pw::span<const uint8_t>(raw.data(), raw.size()));
  EXPECT_EQ(3U, g_status_calls);
  EXPECT_EQ(3U, g_last_status_len);

  Bridge.emitStatus(pw::Status::ResourceExhausted());
  EXPECT_EQ(4U, g_status_calls);
  EXPECT_EQ(static_cast<int>(pw::Status::ResourceExhausted().code()), static_cast<int>(g_last_status.code()));

  Bridge.onStatus(BridgeClass::StatusHandler());
  Bridge.emitStatus(pw::OkStatus(), etl::string_view(""));
  EXPECT_EQ(4U, g_status_calls);
  EXPECT_TRUE(Bridge.isPostPassed());
}

TEST_F(BridgeCoreFixture, BridgeSendResponsesTable) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // GetVersion, GetFreeMemory, GetCapabilities
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("GetVersion"));
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("GetFreeMemory"));
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("GetCapabilities"));

  // DigitalRead & AnalogRead
  rpc_pb_PinRead dr = rpc_pb_PinRead_init_default;
  dr.pin = 13;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("DigitalRead"), rpc_pb_PinRead_fields,
                   dr);
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("AnalogRead"), rpc_pb_PinRead_fields,
                   dr);

  EXPECT_TRUE(Bridge.isSynchronized());
}

TEST_F(BridgeCoreFixture, BridgeDispatchStatusAndUnknown) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  send_hdlc_rpc(ba, 99999, 88888);
  EXPECT_TRUE(Bridge.isSynchronized());
}

TEST_F(BridgeCoreFixture, BridgePostFailure) {
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL);
  Bridge.begin(115200, "secret");
  bridge::test::fault::reset();
  reset_bridge();
  EXPECT_TRUE(Bridge.isPostPassed());
}

TEST_F(BridgeCoreFixture, BridgeFrameErrorsAndZeroSpi) {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Corrupted HDLC frame
  uint8_t malformed[] = {0x7E, 0x01, 0x02, 0x7E};
  ba.feedHdlcBytes(pw::span<const uint8_t>(malformed, sizeof(malformed)));

  // 2. Corrupt CRC frame
  etl::array<uint8_t, 128> frame_raw;
  size_t len = build_hdlc_rpc_frame(
      1, pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.SystemService"),
      pw::tokenizer::Hash("GetVersion"), pw::span<const uint8_t>(), frame_raw);
  if (len > 4) {
    frame_raw[len - 2] ^= 0xFF;
    ba.feedHdlcBytes(pw::span<const uint8_t>(frame_raw.data(), len));
  }

  // 3. Zero-length SPI transfer
  SPIService.begin();
  rpc_pb_SpiTransfer spi_req = rpc_pb_SpiTransfer_init_default;
  spi_req.data.size = 0;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SpiService"),
                   pw::tokenizer::Hash("Transfer"), rpc_pb_SpiTransfer_fields,
                   spi_req);

  EXPECT_TRUE(Bridge.isSynchronized());
}

class FlowControlTestStream : public Stream {
 public:
  int avail = 0;
  int available() override { return avail; }
  int read() override { return -1; }
  int peek() override { return -1; }
  size_t write(uint8_t) override { return 1; }
  size_t write(const uint8_t*, size_t s) override { return s; }
  void flush() override {}
};

TEST_F(BridgeCoreFixture, BridgeSerialTasksAndFlowControl) {
  BiStream base_stream;
  reset_bridge_core(Bridge, base_stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  FlowControlTestStream fs;
  reset_bridge_core(Bridge, fs);
  fs.avail = 100;
  ba.invokeSerialTask();
  fs.avail = 5;
  ba.invokeSerialTask();
  reset_bridge_core(Bridge, base_stream);

  ba.setPendingBaudrate(rpc::RPC_DEFAULT_BAUDRATE);
  ba.onBaudrateChange();
  ba.invokeWatchdog();

  EXPECT_TRUE(Bridge.isPostPassed());
}

TEST_F(BridgeCoreFixture, BridgeHandshakeAuthenticationEdges) {
  etl::array<uint8_t, 32> secret = {0};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> nonce = {0};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag = {0};
  etl::array<uint8_t, 5> bad_tag = {0};
  bool ok = rpc::security::handshake_verify_tag(secret, nonce, bad_tag);
  EXPECT_FALSE(ok);

  const bool empty_tag_ok = rpc::security::handshake_verify_tag(
      secret, nonce, pw::span<const uint8_t>());
  EXPECT_FALSE(empty_tag_ok);

  const bool generated =
      rpc::security::handshake_generate_tag(secret, nonce, out_tag);
  EXPECT_TRUE(generated);
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> valid_tag = {};
  etl::copy_n(out_tag.begin(), valid_tag.size(), valid_tag.begin());
  EXPECT_TRUE(rpc::security::handshake_verify_tag(secret, nonce, valid_tag));

  etl::array<uint8_t, 16> wrong_tag;
  wrong_tag.fill(0xFF);
  bool tag_mismatch_ok =
      rpc::security::handshake_verify_tag(secret, nonce, wrong_tag);
  EXPECT_FALSE(tag_mismatch_ok);
}

TEST_F(BridgeCoreFixture, BridgeFsmResetsAndLinkReset) {
  bridge::fsm::BridgeFsm unstarted_fsm;
  EXPECT_FALSE(unstarted_fsm.isSynchronized());

  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvReset());
  EXPECT_FALSE(Bridge.isSynchronized());

  // LinkReset via RPC
  ba.setSynchronized();
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("LinkReset"));
  EXPECT_FALSE(Bridge.isSynchronized());
}

TEST_F(BridgeCoreFixture, BridgeDispatchExtraBranches) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Set baudrate guards: same baudrate and zero baudrate
  ba.setPendingBaudrate(115200U);
  rpc_pb_SetBaudratePacket baud_msg = rpc_pb_SetBaudratePacket_init_default;
  baud_msg.baudrate = 115200U;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                   pw::tokenizer::Hash("SetBaudrate"),
                   rpc_pb_SetBaudratePacket_fields, baud_msg);

  baud_msg.baudrate = 0U;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                   pw::tokenizer::Hash("SetBaudrate"),
                   rpc_pb_SetBaudratePacket_fields, baud_msg);

  baud_msg.baudrate = 57600U;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                   pw::tokenizer::Hash("SetBaudrate"),
                   rpc_pb_SetBaudratePacket_fields, baud_msg);

  // 2. Enter bootloader
  rpc_pb_EnterBootloader bl_msg = rpc_pb_EnterBootloader_init_default;
  bl_msg.magic = 0xDEAD;  // wrong magic
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                   pw::tokenizer::Hash("EnterBootloader"),
                   rpc_pb_EnterBootloader_fields, bl_msg);

  bl_msg.magic = rpc::RPC_BOOTLOADER_MAGIC;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                   pw::tokenizer::Hash("EnterBootloader"),
                   rpc_pb_EnterBootloader_fields, bl_msg);

  // 3. Digital/Analog write and read out-of-bounds pins
  rpc_pb_DigitalWrite dw = rpc_pb_DigitalWrite_init_default;
  dw.pin = 255U;
  dw.value = 1U;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("DigitalWrite"),
                   rpc_pb_DigitalWrite_fields, dw);

  rpc_pb_AnalogWrite aw = rpc_pb_AnalogWrite_init_default;
  aw.pin = 255U;
  aw.value = 500U;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("AnalogWrite"),
                   rpc_pb_AnalogWrite_fields, aw);

  rpc_pb_PinMode pm = rpc_pb_PinMode_init_default;
  pm.pin = 255U;
  pm.mode = rpc_pb_PinModeType_PIN_OUTPUT;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("SetPinMode"), rpc_pb_PinMode_fields,
                   pm);

  // Unknown pin mode
  pm.pin = 0U;
  pm.mode = static_cast<rpc_pb_PinModeType>(0xFF);
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("SetPinMode"), rpc_pb_PinMode_fields,
                   pm);

  // SPI Service Begin, End
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SpiService"),
                pw::tokenizer::Hash("Begin"));
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SpiService"),
                pw::tokenizer::Hash("End"));

  // ClockSync
  rpc_pb_ClockSyncRequest cs_req = rpc_pb_ClockSyncRequest_init_default;
  cs_req.host_time_us = 1000000ULL;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.ClockSyncService"),
                   pw::tokenizer::Hash("ClockSync"),
                   rpc_pb_ClockSyncRequest_fields, cs_req);

  ba.setPendingBaudrate(0);
  ba.onBaudrateChange();
}

TEST_F(BridgeCoreFixture, BridgeAuthenticatedDigitalWriteFrame) {
  BiStream stream;
  stream.clear();
  constexpr char kSharedSecret[] = "top-secret";
  reset_bridge_core(Bridge, stream, 0U, kSharedSecret);
  resetArduinoStubPinStates();
  auto& ba = TestAccessor::create(Bridge);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> handshake_nonce = {};
  EXPECT_TRUE(perform_test_link_sync(ba, 0xABU, pw::span<uint8_t>(handshake_nonce)));
  EXPECT_TRUE(ba.isSynchronized());

  rpc_pb_DigitalWrite request = rpc_pb_DigitalWrite_init_default;
  request.pin = 13U;
  request.value = 1U;
  uint8_t payload[64];
  pb_ostream_t payload_stream =
      pb_ostream_from_buffer(payload, sizeof(payload));
  EXPECT_TRUE(pb_encode(&payload_stream, rpc_pb_DigitalWrite_fields, &request));

  uint8_t rpc_packet[128];
  const size_t rpc_packet_size = build_rpc_packet(
      pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.GpioService"),
      pw::tokenizer::Hash("DigitalWrite"),
      pw::span<const uint8_t>(payload, payload_stream.bytes_written),
      rpc_packet, sizeof(rpc_packet));
  EXPECT_GT(rpc_packet_size, 0);

  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE * 2U> session_keys = {};
  const auto host_to_mcu_key =
      pw::span<const uint8_t>(session_keys.data(), rpc::RPC_AEAD_KEY_SIZE);
  const auto mcu_to_host_key = pw::span<const uint8_t>(
      session_keys.data() + rpc::RPC_AEAD_KEY_SIZE, rpc::RPC_AEAD_KEY_SIZE);
  const auto secret =
      pw::span<const uint8_t>(reinterpret_cast<const uint8_t*>(kSharedSecret),
                              sizeof(kSharedSecret) - 1U);
  // Derive the keys independently from both nonces (handshake v2).
  rpc_pb_LinkSync link_response = rpc_pb_LinkSync_init_default;
  EXPECT_TRUE(decode_tx_response(stream, rpc_pb_LinkSync_fields, link_response));
  EXPECT_EQ(rpc::RPC_HANDSHAKE_NONCE_LENGTH, link_response.mcu_nonce.size);
  EXPECT_TRUE(rpc::security::derive_session_keys(
      secret, handshake_nonce,
      pw::span<const uint8_t>(link_response.mcu_nonce.bytes,
                              link_response.mcu_nonce.size),
      session_keys));

  constexpr uint64_t kCounter = 1U;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, 1U + rpc::RPC_AEAD_COUNTER_SIZE> aad = {};
  etl::array<uint8_t, 128> envelope = {};
  aad[0] = 1U;
  for (size_t i = 0; i < rpc::RPC_AEAD_COUNTER_SIZE; ++i) {
    const size_t shift = (rpc::RPC_AEAD_COUNTER_SIZE - 1U - i) * 8U;
    const uint8_t counter_byte = static_cast<uint8_t>(kCounter >> shift);
    envelope[i] = counter_byte;
    nonce[4U + i] = counter_byte;
    aad[1U + i] = counter_byte;
  }
  auto ciphertext = pw::span<uint8_t>(
      envelope.data() + rpc::RPC_AEAD_COUNTER_SIZE, rpc_packet_size);
  auto tag = pw::span<uint8_t>(
      envelope.data() + rpc::RPC_AEAD_COUNTER_SIZE + rpc_packet_size,
      rpc::RPC_AEAD_TAG_SIZE);
  EXPECT_TRUE(rpc::security::aead_encrypt(
      host_to_mcu_key, nonce, aad,
      pw::span<const uint8_t>(rpc_packet, rpc_packet_size), ciphertext, tag));

  const size_t envelope_size =
      rpc::RPC_AEAD_COUNTER_SIZE + rpc_packet_size + rpc::RPC_AEAD_TAG_SIZE;
  etl::array<uint8_t, 160> hdlc_buf;
  tag[0] ^= 0x01U;
  size_t len = TestHdlc::encodeUIFrame(
      1, pw::span<const uint8_t>(envelope.data(), envelope_size), hdlc_buf);
  EXPECT_GT(len, 0);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), len));
  EXPECT_NE(OUTPUT, g_arduino_stub_pin_states[13U].mode);

  tag[0] ^= 0x01U;
  len = TestHdlc::encodeUIFrame(
      1, pw::span<const uint8_t>(envelope.data(), envelope_size), hdlc_buf);
  EXPECT_GT(len, 0);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), len));
  Bridge.process();

  EXPECT_TRUE(Bridge.isSynchronized());
  EXPECT_EQ(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  EXPECT_EQ(HIGH, g_arduino_stub_pin_states[13U].value);

  rpc::security::secure_zero(pw::span<uint8_t>(session_keys));
}

TEST_F(BridgeCoreFixture, BridgeReceiveSecurityGateVariants) {
  BiStream stream;
  stream.clear();
  resetArduinoStubPinStates();
  reset_bridge_core(Bridge, stream, 0U, nullptr);
  auto& no_secret_bridge = TestAccessor::create(Bridge);
  no_secret_bridge.setSynchronized();

  // 1. With no shared secret, DigitalWrite succeeds
  rpc_pb_DigitalWrite write_req = rpc_pb_DigitalWrite_init_default;
  write_req.pin = 13U;
  write_req.value = 1U;
  send_rpc_request(no_secret_bridge, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("DigitalWrite"),
                   rpc_pb_DigitalWrite_fields, write_req);
  EXPECT_EQ(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  EXPECT_EQ(HIGH, g_arduino_stub_pin_states[13U].value);

  // 2. With shared secret set, if UNSYNCHRONIZED, non-LinkSync request is
  // rejected and safe state entered
  stream.clear();
  resetArduinoStubPinStates();
  reset_bridge_core(Bridge, stream, 0U, "top-secret");
  auto& secret_bridge = TestAccessor::create(Bridge);
  secret_bridge.clearSynchronized();

  write_req.pin = 14U;
  write_req.value = 1U;
  send_rpc_request(secret_bridge, pw::tokenizer::Hash("rpc.pb.GpioService"),
                   pw::tokenizer::Hash("DigitalWrite"),
                   rpc_pb_DigitalWrite_fields, write_req);

  EXPECT_FALSE(secret_bridge.isSynchronized());
  EXPECT_NE(HIGH, g_arduino_stub_pin_states[14U].value);

  // 3. LinkReset resets synchronized state
  secret_bridge.setSynchronized();
  send_hdlc_rpc(secret_bridge, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("LinkReset"));
  EXPECT_FALSE(secret_bridge.isSynchronized());
}

static bool s_status_called = false;
static void dummy_status_cb(pw::Status, pw::span<const uint8_t>) {
  s_status_called = true;
}

TEST_F(BridgeCoreFixture, BridgeEdgePathsAndPost) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // 1. Serial task null stream
  ba.setNullStream();
  ba.invokeSerialTask();
  Bridge.setStream(stream);

  // 2. Unknown RPC method
  ba.setSynchronized();
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("UnknownMethod"));

  // 3. Status invalid argument with status handler
  s_status_called = false;
  Bridge.onStatus(BridgeClass::StatusHandler::create<&dummy_status_cb>());
  Bridge.emitStatus(pw::Status::InvalidArgument());
  EXPECT_TRUE(s_status_called);
  Bridge.onStatus(BridgeClass::StatusHandler());

  // 4. Stale handshake timeout
  ba.setSynchronized();
  ba.onHandshakeTimeout();

  // 5. Zero pending baudrate
  ba.setPendingBaudrate(0);
  ba.onBaudrateChange();

  // 6. SPI transfer
  SPIService.begin();
  rpc_pb_SpiTransfer spi_req_data = rpc_pb_SpiTransfer_init_default;
  spi_req_data.data.size = 2;
  spi_req_data.data.bytes[0] = 0x11;
  spi_req_data.data.bytes[1] = 0x22;
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SpiService"),
                   pw::tokenizer::Hash("Transfer"), rpc_pb_SpiTransfer_fields,
                   spi_req_data);
  ba.setTxEnabled(false);
  send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SpiService"),
                   pw::tokenizer::Hash("Transfer"), rpc_pb_SpiTransfer_fields,
                   spi_req_data);
  ba.setTxEnabled(true);

  // 7. Baudrate begin with successful POST
  Bridge.begin(115200, "secret");

  reset_bridge_core(Bridge, stream);
}

TEST_F(BridgeCoreFixture, BridgePwLogTokenizedHdlcFrame) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  stream.tx_buf.clear();

  PW_LOG_INFO("Phase2 log value=%d", 42);
  EXPECT_GT(stream.tx_buf.len, 0U);

  pw::hdlc::DecoderBuffer<1024> decoder;
  pw::Result<pw::hdlc::Frame> decoded = pw::Status::NotFound();
  for (size_t i = 0; i < stream.tx_buf.len; ++i) {
    auto res = decoder.Process(
        static_cast<pw::ByteSpan::element_type>(stream.tx_buf.data[i]));
    if (res.ok() && res.value().address() == rpc::RPC_HDLC_ADDRESS_LOG) {
      decoded = res;
      break;
    }
  }
  EXPECT_TRUE(decoded.ok());
  const pw::hdlc::Frame& frame = decoded.value();
  // 4-byte 32-bit token + 1-byte varint argument (ZigZag(42) = 84 = 0x54)
  EXPECT_GE(frame.data().size(), 5U);

  const uint32_t expected_token =
      PW_TOKENIZER_STRING_TOKEN("Phase2 log value=%d");
  uint32_t actual_token = 0;
  memcpy(&actual_token, frame.data().data(), sizeof(uint32_t));
  EXPECT_EQ(expected_token, actual_token);
  EXPECT_EQ(0x54U, static_cast<uint8_t>(frame.data()[4]));
}

TEST_F(BridgeCoreFixture, BridgeSafeStateEmitsTokenizedPwLog) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  stream.tx_buf.clear();

  Bridge.enterSafeState();
  EXPECT_GT(stream.tx_buf.len, 0U);

  pw::hdlc::DecoderBuffer<1024> decoder;
  pw::Result<pw::hdlc::Frame> decoded = pw::Status::NotFound();
  for (size_t i = 0; i < stream.tx_buf.len; ++i) {
    auto res = decoder.Process(
        static_cast<pw::ByteSpan::element_type>(stream.tx_buf.data[i]));
    if (res.ok() && res.value().address() == rpc::RPC_HDLC_ADDRESS_LOG) {
      decoded = res;
      break;
    }
  }
  EXPECT_TRUE(decoded.ok());
  EXPECT_EQ(sizeof(uint32_t), decoded.value().data().size());

  const uint32_t expected_token =
      PW_TOKENIZER_STRING_TOKEN("BRIDGE_SAFE_STATE");
  uint32_t actual_token = 0;
  memcpy(&actual_token, decoded.value().data().data(), sizeof(uint32_t));
  EXPECT_EQ(expected_token, actual_token);
}

namespace {

struct DecodedBridgeMetrics {
  uint32_t seen_mask = 0U;
  uint32_t tx_frames = 0U;
  uint32_t rx_frames = 0U;
  uint32_t safe_state_entries = 0U;
  uint32_t wcet_max_us = 0U;
  uint32_t free_memory_bytes = 0U;
  uint32_t stack_used_bytes = 0U;
};

void decode_single_metric(pw::ConstByteSpan metric_bytes,
                          DecodedBridgeMetrics& out) {
  static constexpr uint32_t kGroupBridge =
      PW_TOKENIZE_STRING_DOMAIN("metrics", "bridge");
  static constexpr uint32_t kTokTxFrames = PW_METRIC_TOKEN("tx_frames");
  static constexpr uint32_t kTokRxFrames = PW_METRIC_TOKEN("rx_frames");
  static constexpr uint32_t kTokSafeState =
      PW_METRIC_TOKEN("safe_state_entries");
  static constexpr uint32_t kTokWcet = PW_METRIC_TOKEN("wcet_max_us");
  static constexpr uint32_t kTokFreeMem = PW_METRIC_TOKEN("free_memory_bytes");
  static constexpr uint32_t kTokStackUsed = PW_METRIC_TOKEN("stack_used_bytes");

  pw::protobuf::Decoder decoder(metric_bytes);
  etl::array<uint32_t, 4> token_path{};
  size_t path_len = 0U;
  uint32_t value = 0U;
  bool has_value = false;

  while (decoder.Next().ok()) {
    const uint32_t field = decoder.FieldNumber();
    if (field == static_cast<uint32_t>(
                     pw::metric::proto::pwpb::Metric::Fields::kTokenPath)) {
      pw::ConstByteSpan packed;
      if (decoder.ReadBytes(&packed).ok()) {
        const size_t count =
            etl::min(packed.size() / sizeof(uint32_t), token_path.size());
        for (size_t i = 0; i < count; ++i) {
          token_path[i] = pw::bytes::ReadInOrder<uint32_t>(
              pw::endian::little, packed.data() + (i * sizeof(uint32_t)));
        }
        path_len = count;
      }
    } else if (field == static_cast<uint32_t>(
                            pw::metric::proto::pwpb::Metric::Fields::kAsInt)) {
      if (decoder.ReadUint32(&value).ok()) {
        has_value = true;
      }
    }
  }

  if (!has_value || path_len != 2U || token_path[0] != kGroupBridge) {
    return;
  }
  const uint32_t metric_tok = token_path[1];
  if (metric_tok == kTokTxFrames) {
    out.seen_mask |= (1U << 0U);
    out.tx_frames = value;
  } else if (metric_tok == kTokRxFrames) {
    out.seen_mask |= (1U << 1U);
    out.rx_frames = value;
  } else if (metric_tok == kTokSafeState) {
    out.seen_mask |= (1U << 2U);
    out.safe_state_entries = value;
  } else if (metric_tok == kTokWcet) {
    out.seen_mask |= (1U << 3U);
    out.wcet_max_us = value;
  } else if (metric_tok == kTokFreeMem) {
    out.seen_mask |= (1U << 4U);
    out.free_memory_bytes = value;
  } else if (metric_tok == kTokStackUsed) {
    out.seen_mask |= (1U << 5U);
    out.stack_used_bytes = value;
  }
}

void decode_metrics_container(pw::ConstByteSpan payload, uint32_t metrics_field,
                              DecodedBridgeMetrics& out,
                              bool* done_flag = nullptr) {
  pw::protobuf::Decoder decoder(payload);
  while (decoder.Next().ok()) {
    if (decoder.FieldNumber() == metrics_field) {
      pw::ConstByteSpan metric_bytes;
      if (decoder.ReadBytes(&metric_bytes).ok()) {
        decode_single_metric(metric_bytes, out);
      }
    } else if (done_flag != nullptr &&
               decoder.FieldNumber() ==
                   static_cast<uint32_t>(
                       pw::metric::proto::pwpb::WalkResponse::Fields::kDone)) {
      bool done = false;
      if (decoder.ReadBool(&done).ok()) {
        *done_flag = done;
      }
    }
  }
}

}  // namespace

TEST_F(BridgeCoreFixture, BridgePwMetricServiceGetAndWalk) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Drive observable metric updates: WCET, TX frames, and safe-state entry.
  Bridge.process();
  const etl::array<uint8_t, 4> console_bytes = {'P', 'I', 'N', 'G'};
  EXPECT_TRUE(Bridge.sendConsoleStream(
      pw::span<const uint8_t>(console_bytes.data(), console_bytes.size())));
  Bridge.enterSafeState();
  ba.setTxEnabled(true);
  ba.setSynchronized();
  stream.tx_buf.clear();

  // 1. Invoke pw.metric.proto.MetricService/Get (server streaming).
  EXPECT_GT(send_hdlc_rpc(ba, pw::tokenizer::Hash("pw.metric.proto.MetricService"),
                    pw::tokenizer::Hash("Get")), 0U);

  DecodedBridgeMetrics get_metrics{};
  size_t stream_packets = 0U;
  bool got_finish_ok = false;
  pw::hdlc::DecoderBuffer<1024> decoder;
  for (size_t i = 0; i < stream.tx_buf.len; ++i) {
    auto frame_res = decoder.Process(
        static_cast<pw::ByteSpan::element_type>(stream.tx_buf.data[i]));
    if (!frame_res.ok()) continue;
    const auto& frame = frame_res.value();
    if (frame.address() != rpc::RPC_HDLC_ADDRESS_RPC) continue;
    auto pkt_res = pw::rpc::internal::Packet::FromBuffer(frame.data());
    EXPECT_TRUE(pkt_res.ok());
    const auto& pkt = pkt_res.value();
    if (pkt.type() == pw::rpc::internal::pwpb::PacketType::SERVER_STREAM) {
      ++stream_packets;
      decode_metrics_container(
          pkt.payload(),
          static_cast<uint32_t>(
              pw::metric::proto::pwpb::MetricResponse::Fields::kMetrics),
          get_metrics);
    } else if (pkt.type() == pw::rpc::internal::pwpb::PacketType::RESPONSE) {
      EXPECT_TRUE(pkt.status().ok());
      got_finish_ok = true;
    }
  }

  EXPECT_GE(stream_packets, 2U);
  EXPECT_TRUE(got_finish_ok);
  EXPECT_EQ(0x3FU, get_metrics.seen_mask);
  EXPECT_GE(get_metrics.tx_frames, 2U);
  EXPECT_GE(get_metrics.rx_frames, 1U);
  EXPECT_EQ(1U, get_metrics.safe_state_entries);
  EXPECT_EQ(Bridge.getWcetMaxMicros(), get_metrics.wcet_max_us);
  EXPECT_GE(get_metrics.free_memory_bytes, bridge::hal::MIN_STACK_MARGIN_BYTES);
  // Host HAL has no runtime stack watermark; UINT32_MAX is the explicit
  // unavailable sentinel.
  EXPECT_EQ(static_cast<uint32_t>(-1), get_metrics.stack_used_bytes);

  // 2. Invoke pw.metric.proto.MetricService/Walk (paginated unary).
  stream.tx_buf.clear();
  EXPECT_GT(send_hdlc_rpc(ba, pw::tokenizer::Hash("pw.metric.proto.MetricService"),
                    pw::tokenizer::Hash("Walk")), 0U);

  DecodedBridgeMetrics walk_metrics{};
  bool walk_done = false;
  bool walk_response_ok = false;
  pw::hdlc::DecoderBuffer<1024> walk_decoder;
  for (size_t i = 0; i < stream.tx_buf.len; ++i) {
    auto frame_res = walk_decoder.Process(
        static_cast<pw::ByteSpan::element_type>(stream.tx_buf.data[i]));
    if (!frame_res.ok()) continue;
    const auto& frame = frame_res.value();
    if (frame.address() != rpc::RPC_HDLC_ADDRESS_RPC) continue;
    auto pkt_res = pw::rpc::internal::Packet::FromBuffer(frame.data());
    EXPECT_TRUE(pkt_res.ok());
    const auto& pkt = pkt_res.value();
    if (pkt.type() == pw::rpc::internal::pwpb::PacketType::RESPONSE) {
      EXPECT_TRUE(pkt.status().ok());
      walk_response_ok = true;
      decode_metrics_container(
          pkt.payload(),
          static_cast<uint32_t>(
              pw::metric::proto::pwpb::WalkResponse::Fields::kMetrics),
          walk_metrics, &walk_done);
    }
  }
  EXPECT_TRUE(walk_response_ok);
  EXPECT_TRUE(walk_done);
  EXPECT_EQ(0x3FU, walk_metrics.seen_mask);
  EXPECT_GT(walk_metrics.rx_frames, get_metrics.rx_frames);
}

namespace {

bool extract_last_rpc_response_status(const BiStream& stream,
                                      uint32_t service_id, uint32_t method_id,
                                      pw::Status& out_status) {
  pw::hdlc::DecoderBuffer<1024> decoder;
  bool found = false;
  for (size_t i = 0; i < stream.tx_buf.len; ++i) {
    auto frame_res = decoder.Process(
        static_cast<pw::ByteSpan::element_type>(stream.tx_buf.data[i]));
    if (!frame_res.ok() ||
        frame_res.value().address() != rpc::RPC_HDLC_ADDRESS_RPC) {
      continue;
    }
    auto pkt_res =
        pw::rpc::internal::Packet::FromBuffer(frame_res.value().data());
    if (pkt_res.ok() &&
        pkt_res.value().type() ==
            pw::rpc::internal::pwpb::PacketType::RESPONSE &&
        pkt_res.value().service_id() == service_id &&
        pkt_res.value().method_id() == method_id) {
      found = true;
      out_status = pkt_res.value().status();
    }
  }
  return found;
}

}  // namespace

// Regression (R25): unsubscribing a pin must reclaim its slot in
// _pin_subscriptions so subscribing a new pin after reaching
// MAX_PIN_SUBSCRIPTIONS succeeds.
TEST_F(BridgeCoreFixture, PinSubscribeUnsubscribeReclaimsSlot) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  resetArduinoStubPinStates();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  constexpr uint32_t kGpioSvc = pw::tokenizer::Hash("rpc.pb.GpioService");
  constexpr uint32_t kSubMeth = pw::tokenizer::Hash("SubscribePins");

  for (uint8_t i = 0; i < bridge::config::MAX_PIN_SUBSCRIPTIONS; ++i) {
    rpc_pb_PinSubscribeRequest req = rpc_pb_PinSubscribeRequest_init_default;
    req.pin = static_cast<uint32_t>(2U + i);
    req.mode = rpc_pb_PinModeType_PIN_INPUT;
    req.interval_ms = 50U;
    req.hysteresis = 1U;
    req.enabled = true;
    send_rpc_request(ba, kGpioSvc, kSubMeth, rpc_pb_PinSubscribeRequest_fields,
                     req);
    EXPECT_GE(ba.subscriptionLastValueForPin(static_cast<uint8_t>(2U + i)), 0);
  }

  // Unsubscribe one pin in the middle and verify its slot is erased.
  stream.tx_buf.clear();
  rpc_pb_PinSubscribeRequest unsub = rpc_pb_PinSubscribeRequest_init_default;
  unsub.pin = 5U;
  unsub.enabled = false;
  send_rpc_request(ba, kGpioSvc, kSubMeth, rpc_pb_PinSubscribeRequest_fields,
                   unsub);
  EXPECT_EQ(-1, ba.subscriptionLastValueForPin(5U));

  // Subscribing a brand-new pin must now reuse the reclaimed capacity.
  stream.tx_buf.clear();
  constexpr uint8_t kNewPin =
      static_cast<uint8_t>(2U + bridge::config::MAX_PIN_SUBSCRIPTIONS);
  rpc_pb_PinSubscribeRequest sub_new = rpc_pb_PinSubscribeRequest_init_default;
  sub_new.pin = kNewPin;
  sub_new.mode = rpc_pb_PinModeType_PIN_INPUT_PULLUP;
  sub_new.interval_ms = 50U;
  sub_new.hysteresis = 1U;
  sub_new.enabled = true;
  send_rpc_request(ba, kGpioSvc, kSubMeth, rpc_pb_PinSubscribeRequest_fields,
                   sub_new);
  pw::Status resp_status = pw::OkStatus();
  EXPECT_FALSE(extract_last_rpc_response_status(stream, kGpioSvc, kSubMeth,
                                                     resp_status));
  EXPECT_GE(ba.subscriptionLastValueForPin(kNewPin), 0);
  EXPECT_EQ(INPUT_PULLUP, g_arduino_stub_pin_states[kNewPin].mode);
}

// Regression (R25): when _pin_subscriptions is full, rejecting a new pin
// subscribe request must return ResourceExhausted AND must not call ::pinMode()
// on the rejected pin.
TEST_F(BridgeCoreFixture, PinSubscribeFullDoesNotMutatePinMode) {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  resetArduinoStubPinStates();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  constexpr uint32_t kGpioSvc = pw::tokenizer::Hash("rpc.pb.GpioService");
  constexpr uint32_t kSubMeth = pw::tokenizer::Hash("SubscribePins");

  for (uint8_t i = 0; i < bridge::config::MAX_PIN_SUBSCRIPTIONS; ++i) {
    rpc_pb_PinSubscribeRequest req = rpc_pb_PinSubscribeRequest_init_default;
    req.pin = static_cast<uint32_t>(2U + i);
    req.mode = rpc_pb_PinModeType_PIN_INPUT;
    req.interval_ms = 50U;
    req.hysteresis = 1U;
    req.enabled = true;
    send_rpc_request(ba, kGpioSvc, kSubMeth, rpc_pb_PinSubscribeRequest_fields,
                     req);
  }

  constexpr uint8_t kRejectedPullupPin =
      static_cast<uint8_t>(2U + bridge::config::MAX_PIN_SUBSCRIPTIONS);
  constexpr uint8_t kRejectedInputPin =
      static_cast<uint8_t>(3U + bridge::config::MAX_PIN_SUBSCRIPTIONS);
  EXPECT_EQ(0xFFU, g_arduino_stub_pin_states[kRejectedPullupPin].mode);
  EXPECT_EQ(0xFFU, g_arduino_stub_pin_states[kRejectedInputPin].mode);

  stream.tx_buf.clear();
  rpc_pb_PinSubscribeRequest rej_pullup =
      rpc_pb_PinSubscribeRequest_init_default;
  rej_pullup.pin = kRejectedPullupPin;
  rej_pullup.mode = rpc_pb_PinModeType_PIN_INPUT_PULLUP;
  rej_pullup.enabled = true;
  send_rpc_request(ba, kGpioSvc, kSubMeth, rpc_pb_PinSubscribeRequest_fields,
                   rej_pullup);
  pw::Status status_pullup = pw::OkStatus();
  EXPECT_TRUE(extract_last_rpc_response_status(stream, kGpioSvc, kSubMeth,
                                                    status_pullup));
  EXPECT_EQ(static_cast<int>(pw::Status::ResourceExhausted().code()), static_cast<int>(status_pullup.code()));
  EXPECT_EQ(0xFFU, g_arduino_stub_pin_states[kRejectedPullupPin].mode);
  EXPECT_EQ(-1, ba.subscriptionLastValueForPin(kRejectedPullupPin));

  stream.tx_buf.clear();
  rpc_pb_PinSubscribeRequest rej_input =
      rpc_pb_PinSubscribeRequest_init_default;
  rej_input.pin = kRejectedInputPin;
  rej_input.mode = rpc_pb_PinModeType_PIN_INPUT;
  rej_input.enabled = true;
  send_rpc_request(ba, kGpioSvc, kSubMeth, rpc_pb_PinSubscribeRequest_fields,
                   rej_input);
  pw::Status status_input = pw::OkStatus();
  EXPECT_TRUE(extract_last_rpc_response_status(stream, kGpioSvc, kSubMeth,
                                                    status_input));
  EXPECT_EQ(static_cast<int>(pw::Status::ResourceExhausted().code()), static_cast<int>(status_input.code()));
  EXPECT_EQ(0xFFU, g_arduino_stub_pin_states[kRejectedInputPin].mode);
  EXPECT_EQ(-1, ba.subscriptionLastValueForPin(kRejectedInputPin));
}

