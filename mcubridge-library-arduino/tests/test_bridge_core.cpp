#ifndef ETL_FORCE_STD_INITIALIZER_LIST
#define ETL_FORCE_STD_INITIALIZER_LIST
#endif

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <Arduino.h>
#include <BridgeFaultInjection.h>
#include <etl/array.h>
#include <etl/numeric.h>
#include <etl/string_view.h>
#include <pw_span/span.h>
#include <unity.h>

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

void setUp() {}
void tearDown() { Bridge.setStream(Serial); }

void reset_bridge(const char* secret = nullptr) {
  Bridge.begin(115200, secret);
  Console.begin();
}

void test_bridge_initialization() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());
}

void test_bridge_handshake() {
  reset_bridge("6368616e67656d65313233");
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  TEST_ASSERT_TRUE(perform_test_link_sync(ba, 0x42));
  Bridge.process();

  TEST_ASSERT_TRUE(ba.isSynchronized());
}

void test_bridge_handshake_response_uses_tag_length() {
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
  TEST_ASSERT_TRUE(
      rpc::security::handshake_generate_tag(secret, nonce, request_tag));

  rpc_pb_LinkSync request = rpc_pb_LinkSync_init_default;
  etl::copy_n(nonce.begin(), nonce.size(), request.nonce.bytes);
  request.nonce.size = nonce.size();
  etl::copy_n(request_tag.begin(), request_tag.size(), request.tag.bytes);
  request.tag.size = request_tag.size();

  TEST_ASSERT_GREATER_THAN(
      0, send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                          pw::tokenizer::Hash("LinkSync"),
                          rpc_pb_LinkSync_fields, request));

  rpc_pb_LinkSync response = rpc_pb_LinkSync_init_default;
  TEST_ASSERT_TRUE(
      decode_tx_response(stream, rpc_pb_LinkSync_fields, response));
  TEST_ASSERT_EQUAL_UINT32(rpc::RPC_HANDSHAKE_TAG_LENGTH, response.tag.size);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> expected_response_tag = {};
  TEST_ASSERT_TRUE(rpc::security::handshake_generate_response_tag(
      secret, nonce, expected_response_tag));
  TEST_ASSERT_EQUAL_UINT8_ARRAY(expected_response_tag.data(),
                                response.tag.bytes,
                                expected_response_tag.size());
}

void test_bridge_send_frame() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> payload = {0xAA, 0xBB};
  TEST_ASSERT_TRUE(
      Bridge.sendConsoleStream(pw::span<const uint8_t>(payload.data(), 2)));
  TEST_ASSERT_TRUE(
      Bridge.sendLogStream(pw::span<const uint8_t>(payload.data(), 2)));
  pw_log_avr_HandleLog(1, payload.data(), payload.size());

  rpc_pb_PinSubscribeRequest sub_req = rpc_pb_PinSubscribeRequest_init_default;
  sub_req.pin = 13;
  sub_req.enabled = true;
  TEST_ASSERT_GREATER_THAN(
      0, send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                          pw::tokenizer::Hash("SubscribePins"),
                          rpc_pb_PinSubscribeRequest_fields, sub_req));

  TEST_ASSERT_TRUE(Bridge.sendPinEvent(13, 1));
}

void test_bridge_process_rx() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_DigitalWrite msg = rpc_pb_DigitalWrite_init_default;
  msg.pin = 13;
  msg.value = 1;
  TEST_ASSERT_GREATER_THAN(
      0, send_rpc_request(ba, pw::tokenizer::Hash("rpc.pb.GpioService"),
                          pw::tokenizer::Hash("DigitalWrite"),
                          rpc_pb_DigitalWrite_fields, msg));
  Bridge.process();

  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  TEST_ASSERT_EQUAL_UINT8(HIGH, g_arduino_stub_pin_states[13U].value);
}

void test_bridge_dedup_console_write() {
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
  TEST_ASSERT_EQUAL(4, Console.available());
}

void test_bridge_status_ack() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Verify RpcPacket response routing
  pw::rpc::internal::Packet pkt(pw::rpc::internal::pwpb::PacketType::RESPONSE,
                                1, pw::tokenizer::Hash("rpc.pb.SystemService"),
                                pw::tokenizer::Hash("GetVersion"), 0,
                                pw::ConstByteSpan());
  ba.invokeRpcResponse(pkt);
  TEST_ASSERT_TRUE(ba.isSynchronized());
}

void test_bridge_post_and_stack_sentinel() {
  TEST_ASSERT_TRUE(bridge::hal::run_power_on_self_tests());
  TEST_ASSERT_TRUE(bridge::hal::checkStackOverflow());
  TEST_ASSERT_GREATER_OR_EQUAL_UINT32(bridge::hal::MIN_STACK_MARGIN_BYTES,
                                      bridge::hal::getFreeMemory());
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_await_synchronization_bounded() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);

  const uint32_t start_ms = bridge::test::fault::clock_ms();
  TEST_ASSERT_FALSE(Bridge.awaitSynchronization(50U, 5U));
  TEST_ASSERT_GREATER_OR_EQUAL_UINT32(
      50U, bridge::test::fault::clock_ms() - start_ms);
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
  TEST_ASSERT_FALSE(ba.isTxEnabled());

  TEST_ASSERT_FALSE(Bridge.awaitSynchronization(3U, 0U));

  reset_bridge();
  ba.setSynchronized();
  const uint32_t sync_start_ms = bridge::test::fault::clock_ms();
  TEST_ASSERT_TRUE(Bridge.awaitSynchronization(50U, 5U));
  TEST_ASSERT_EQUAL_UINT32(sync_start_ms, bridge::test::fault::clock_ms());
  TEST_ASSERT_TRUE(ba.isTxEnabled());
}

void test_bridge_wcet_tracking() {
  Bridge.resetWcetStats();
  TEST_ASSERT_EQUAL_UINT32(0, Bridge.getWcetMaxMicros());
  Bridge.process();
  TEST_ASSERT_GREATER_OR_EQUAL_UINT32(0, Bridge.getWcetMaxMicros());
}

void test_bridge_begin_null_secret() {
  Bridge.begin(115200, nullptr);
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
  Bridge.process();
}

void test_bridge_handshake_timeout_not_sync() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  ba.onHandshakeTimeout();
  TEST_ASSERT_TRUE(ba.isFault());

  reset_bridge();
  ba.setSynchronized();
  ba.onHandshakeTimeout();
  TEST_ASSERT_FALSE(ba.isFault());
}

namespace {
rpc::StatusCode g_last_status = rpc::StatusCode::STATUS_OK;
size_t g_last_status_len = 0U;
size_t g_status_calls = 0U;
void capture_status_handler(rpc::StatusCode s, pw::span<const uint8_t> p) {
  g_last_status = s;
  g_last_status_len = p.size();
  ++g_status_calls;
}
}  // namespace

void test_bridge_queue_full_and_flow_control() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 10> cdata{};
  cdata.fill('c');
  TEST_ASSERT_TRUE(Bridge.sendConsoleStream(cdata));
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_bridge_emit_status_variants() {
  reset_bridge();
  g_status_calls = 0U;
  Bridge.onStatus(BridgeClass::StatusHandler::create<capture_status_handler>());

  Bridge.emitStatus(rpc::StatusCode::STATUS_OK);
  TEST_ASSERT_EQUAL_UINT32(1U, g_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(rpc::StatusCode::STATUS_OK),
                        static_cast<int>(g_last_status));
  TEST_ASSERT_EQUAL_UINT32(0U, g_last_status_len);

  Bridge.emitStatus(rpc::StatusCode::STATUS_INTERNAL,
                    etl::string_view("Error"));
  TEST_ASSERT_EQUAL_UINT32(2U, g_status_calls);
  TEST_ASSERT_EQUAL_INT(static_cast<int>(rpc::StatusCode::STATUS_INTERNAL),
                        static_cast<int>(g_last_status));
  TEST_ASSERT_EQUAL_UINT32(5U, g_last_status_len);

  const etl::array<uint8_t, 3> raw = {1U, 2U, 3U};
  Bridge.emitStatus(rpc::StatusCode::STATUS_INVALID_ARGUMENT,
                    pw::span<const uint8_t>(raw.data(), raw.size()));
  TEST_ASSERT_EQUAL_UINT32(3U, g_status_calls);
  TEST_ASSERT_EQUAL_UINT32(3U, g_last_status_len);

  Bridge.emitStatus(pw::Status::ResourceExhausted());
  TEST_ASSERT_EQUAL_UINT32(4U, g_status_calls);
  TEST_ASSERT_EQUAL_INT(
      static_cast<int>(rpc::StatusCode::STATUS_RESOURCE_EXHAUSTED),
      static_cast<int>(g_last_status));

  Bridge.onStatus(BridgeClass::StatusHandler());
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, etl::string_view(""));
  TEST_ASSERT_EQUAL_UINT32(4U, g_status_calls);
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_send_responses_table() {
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

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_bridge_dispatch_status_and_unknown() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  send_hdlc_rpc(ba, 99999, 88888);
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_bridge_post_failure() {
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::BRIDGE_FORCE_POST_FAIL);
  Bridge.begin(115200, "secret");
  bridge::test::fault::reset();
  reset_bridge();
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_frame_errors_and_zero_spi() {
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

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
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

void test_bridge_serial_tasks_and_flow_control() {
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

  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_handshake_authentication_edges() {
  etl::array<uint8_t, 32> secret = {0};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> nonce = {0};
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_HKDF_OUTPUT_LENGTH> out_tag = {0};
  etl::array<uint8_t, 5> bad_tag = {0};
  bool ok = rpc::security::handshake_verify_tag(secret, nonce, bad_tag);
  TEST_ASSERT_FALSE(ok);

  const bool empty_tag_ok = rpc::security::handshake_verify_tag(
      secret, nonce, pw::span<const uint8_t>());
  TEST_ASSERT_FALSE(empty_tag_ok);

  const bool generated =
      rpc::security::handshake_generate_tag(secret, nonce, out_tag);
  TEST_ASSERT_TRUE(generated);
  etl::array<uint8_t, rpc::RPC_HANDSHAKE_TAG_LENGTH> valid_tag = {};
  etl::copy_n(out_tag.begin(), valid_tag.size(), valid_tag.begin());
  TEST_ASSERT_TRUE(
      rpc::security::handshake_verify_tag(secret, nonce, valid_tag));

  etl::array<uint8_t, 16> wrong_tag;
  wrong_tag.fill(0xFF);
  bool tag_mismatch_ok =
      rpc::security::handshake_verify_tag(secret, nonce, wrong_tag);
  TEST_ASSERT_FALSE(tag_mismatch_ok);
}

void test_bridge_fsm_resets_and_link_reset() {
  bridge::fsm::BridgeFsm unstarted_fsm;
  TEST_ASSERT_FALSE(unstarted_fsm.isSynchronized());

  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvReset());
  TEST_ASSERT_FALSE(Bridge.isSynchronized());

  // LinkReset via RPC
  ba.setSynchronized();
  send_hdlc_rpc(ba, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("LinkReset"));
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

void test_bridge_dispatch_extra_branches() {
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

void test_bridge_authenticated_digital_write_frame() {
  BiStream stream;
  stream.clear();
  constexpr char kSharedSecret[] = "top-secret";
  reset_bridge_core(Bridge, stream, 0U, kSharedSecret);
  resetArduinoStubPinStates();
  auto& ba = TestAccessor::create(Bridge);

  etl::array<uint8_t, rpc::RPC_HANDSHAKE_NONCE_LENGTH> handshake_nonce = {};
  TEST_ASSERT_TRUE(
      perform_test_link_sync(ba, 0xABU, pw::span<uint8_t>(handshake_nonce)));
  TEST_ASSERT_TRUE(ba.isSynchronized());

  rpc_pb_DigitalWrite request = rpc_pb_DigitalWrite_init_default;
  request.pin = 13U;
  request.value = 1U;
  uint8_t payload[64];
  pb_ostream_t payload_stream =
      pb_ostream_from_buffer(payload, sizeof(payload));
  TEST_ASSERT_TRUE(
      pb_encode(&payload_stream, rpc_pb_DigitalWrite_fields, &request));

  uint8_t rpc_packet[128];
  const size_t rpc_packet_size = build_rpc_packet(
      pw::rpc::internal::pwpb::PacketType::REQUEST,
      pw::tokenizer::Hash("rpc.pb.GpioService"),
      pw::tokenizer::Hash("DigitalWrite"),
      pw::span<const uint8_t>(payload, payload_stream.bytes_written),
      rpc_packet, sizeof(rpc_packet));
  TEST_ASSERT_GREATER_THAN(0, rpc_packet_size);

  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE * 2U> session_keys = {};
  const auto host_to_mcu_key =
      pw::span<const uint8_t>(session_keys.data(), rpc::RPC_AEAD_KEY_SIZE);
  const auto mcu_to_host_key = pw::span<const uint8_t>(
      session_keys.data() + rpc::RPC_AEAD_KEY_SIZE, rpc::RPC_AEAD_KEY_SIZE);
  const auto secret =
      pw::span<const uint8_t>(reinterpret_cast<const uint8_t*>(kSharedSecret),
                              sizeof(kSharedSecret) - 1U);
  TEST_ASSERT_TRUE(rpc::security::derive_session_keys(
      secret, handshake_nonce, session_keys));

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
  TEST_ASSERT_TRUE(rpc::security::aead_encrypt(
      host_to_mcu_key, nonce, aad,
      pw::span<const uint8_t>(rpc_packet, rpc_packet_size), ciphertext, tag));

  const size_t envelope_size =
      rpc::RPC_AEAD_COUNTER_SIZE + rpc_packet_size + rpc::RPC_AEAD_TAG_SIZE;
  etl::array<uint8_t, 160> hdlc_buf;
  tag[0] ^= 0x01U;
  size_t len = TestHdlc::encodeUIFrame(
      1, pw::span<const uint8_t>(envelope.data(), envelope_size), hdlc_buf);
  TEST_ASSERT_GREATER_THAN(0, len);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), len));
  TEST_ASSERT_NOT_EQUAL(OUTPUT, g_arduino_stub_pin_states[13U].mode);

  tag[0] ^= 0x01U;
  len = TestHdlc::encodeUIFrame(
      1, pw::span<const uint8_t>(envelope.data(), envelope_size), hdlc_buf);
  TEST_ASSERT_GREATER_THAN(0, len);
  ba.feedHdlcBytes(pw::span<const uint8_t>(hdlc_buf.data(), len));
  Bridge.process();

  TEST_ASSERT_TRUE(Bridge.isSynchronized());
  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  TEST_ASSERT_EQUAL_UINT8(HIGH, g_arduino_stub_pin_states[13U].value);

  rpc::security::secure_zero(pw::span<uint8_t>(session_keys));
}

void test_bridge_receive_security_gate_variants() {
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
  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  TEST_ASSERT_EQUAL_UINT8(HIGH, g_arduino_stub_pin_states[13U].value);

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

  TEST_ASSERT_FALSE(secret_bridge.isSynchronized());
  TEST_ASSERT_NOT_EQUAL(HIGH, g_arduino_stub_pin_states[14U].value);

  // 3. LinkReset resets synchronized state
  secret_bridge.setSynchronized();
  send_hdlc_rpc(secret_bridge, pw::tokenizer::Hash("rpc.pb.SystemService"),
                pw::tokenizer::Hash("LinkReset"));
  TEST_ASSERT_FALSE(secret_bridge.isSynchronized());
}

static bool s_status_called = false;
static void dummy_status_cb(rpc::StatusCode, pw::span<const uint8_t>) {
  s_status_called = true;
}

void test_bridge_fault_injection_kat_and_post() {
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
  Bridge.emitStatus(rpc::StatusCode::STATUS_INVALID_ARGUMENT);
  TEST_ASSERT_TRUE(s_status_called);
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

  // 7. Baudrate begin with failed POST
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::KAT_SHA256_MISMATCH);
  Bridge.begin(115200, "secret");
  bridge::test::fault::disable(
      bridge::test::fault::FaultPoint::KAT_SHA256_MISMATCH);

  reset_bridge_core(Bridge, stream);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_initialization);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_handshake_response_uses_tag_length);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  RUN_TEST(test_bridge_dedup_console_write);
  RUN_TEST(test_bridge_status_ack);
  RUN_TEST(test_bridge_post_and_stack_sentinel);
  RUN_TEST(test_bridge_await_synchronization_bounded);
  RUN_TEST(test_bridge_wcet_tracking);
  RUN_TEST(test_bridge_begin_null_secret);
  RUN_TEST(test_bridge_handshake_timeout_not_sync);
  RUN_TEST(test_bridge_queue_full_and_flow_control);
  RUN_TEST(test_bridge_emit_status_variants);
  RUN_TEST(test_bridge_send_responses_table);
  RUN_TEST(test_bridge_dispatch_status_and_unknown);
  RUN_TEST(test_bridge_post_failure);
  RUN_TEST(test_bridge_frame_errors_and_zero_spi);
  RUN_TEST(test_bridge_serial_tasks_and_flow_control);
  RUN_TEST(test_bridge_handshake_authentication_edges);
  RUN_TEST(test_bridge_fsm_resets_and_link_reset);
  RUN_TEST(test_bridge_dispatch_extra_branches);
  RUN_TEST(test_bridge_authenticated_digital_write_frame);
  RUN_TEST(test_bridge_receive_security_gate_variants);
  RUN_TEST(test_bridge_fault_injection_kat_and_post);
  return UNITY_END();
}
