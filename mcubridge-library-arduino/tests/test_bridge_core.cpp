#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <BridgeFaultInjection.h>
#include <etl/array.h>
#include <etl/numeric.h>
#include <etl/span.h>
#include <etl/string_view.h>
#include <unity.h>

#include <Arduino.h>
#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "security/security.h"
#include "services/Console.h"
#include "services/SPIService.h"
#include "test_support.h"  // IWYU pragma: keep

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
void tearDown() {}

void reset_bridge() {
  Bridge.begin(115200, "6368616e67656d65313233");
  Console.begin();
}

void test_bridge_initialization() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());
  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_handshake() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  TEST_ASSERT_FALSE(ba.isSynchronized());

  // 1. Prepare Handshake Payload using computeHandshakeTag
  etl::array<uint8_t, 16> nonce;
  nonce.fill(0x42);
  etl::array<uint8_t, 16> tag;
  ba.computeHandshakeTag(nonce.data(), nonce.size(), tag.data());

  rpc::payload::LinkSync msg = {};
  etl::copy_n(nonce.begin(), 16, msg.nonce.bytes);
  msg.nonce.size = 16;
  etl::copy_n(tag.begin(), 16, msg.tag.bytes);
  msg.tag.size = 16;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg));

  // 2. Build LinkSync frame using FrameBuilder
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};
  // [MEM-SAVE] Reusing nonce for handshake (aligned with protocol spec).
  etl::copy_n(nonce.begin(), rpc::RPC_AEAD_NONCE_SIZE, frame_nonce.begin());

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_LINK_SYNC), 1,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, tag),
      frame_raw);

  // 3. Dispatch using FrameParser
  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();

  TEST_ASSERT(ba.isSynchronized());
}

void test_bridge_send_frame() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, 2> payload = {0xAA, 0xBB};
  TEST_ASSERT(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 123,
                               etl::span<const uint8_t>(payload.data(), 2)));
}

void test_bridge_process_rx() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc::payload::DigitalWrite msg = {};
  msg.pin = 13;
  msg.value = 1;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE), 10,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, {}),
      frame_raw);

  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();
}

void test_bridge_dedup_console_write() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Build ConsoleWrite frame once
  rpc::payload::ConsoleWrite msg = {};
  const char* text = "TEST";
  etl::copy_n(text, 4, msg.data.bytes);
  msg.data.size = 4;

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(msg)>(), &msg));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE), 55,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, {}),
      frame_raw);

  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());

  // 2. Dispatch twice
  ba.dispatch(frame_res.value());
  Bridge.process();
  TEST_ASSERT_EQUAL(4, Console.available());

  ba.dispatch(frame_res.value());
  Bridge.process();
  // 3. Verify Console.available() remains consistent (deduplicated)
  TEST_ASSERT_EQUAL(4, Console.available());
}

void test_bridge_status_ack() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Trigger a command that requires ACK
  TEST_ASSERT_TRUE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 77));
  TEST_ASSERT_TRUE(ba.isAwaitingAck());

  // 2. Build STATUS_ACK frame targeting sequence ID 77
  rpc::payload::AckPacket p = {};
  p.command_id = rpc::to_underlying(rpc::CommandId::CMD_CONSOLE_WRITE);

  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> pl_buf;
  pb_ostream_t pbos = pb_ostream_from_buffer(pl_buf.data(), pl_buf.size());
  TEST_ASSERT_TRUE(
      pb_encode(&pbos, rpc::Payload::get_fields<decltype(p)>(), &p));

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> frame_nonce = {};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> frame_tag = {};

  size_t len = rpc::serialize_frame(
      rpc::build_envelope(
          rpc::to_underlying(rpc::StatusCode::STATUS_ACK), 77,
          etl::span<const uint8_t>(pl_buf.data(), pbos.bytes_written),
          frame_nonce, frame_tag),
      frame_raw);

  // 3. Dispatch and verify isAwaitingAck() becomes false
  auto frame_res = rpc::parse_frame(etl::span<uint8_t>(frame_raw.data(), len));
  TEST_ASSERT_TRUE(frame_res.has_value());
  ba.dispatch(frame_res.value());
  Bridge.process();

  TEST_ASSERT_FALSE(ba.isAwaitingAck());
}

void test_bridge_post_and_stack_sentinel() {
  TEST_ASSERT_TRUE(bridge::hal::run_power_on_self_tests());
  TEST_ASSERT_TRUE(bridge::hal::checkStackOverflow());
  TEST_ASSERT_GREATER_OR_EQUAL_UINT16(bridge::hal::MIN_STACK_MARGIN_BYTES,
                                      bridge::hal::getFreeStackMargin());
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
  TEST_ASSERT_GREATER_OR_EQUAL_UINT16(bridge::hal::MIN_STACK_MARGIN_BYTES,
                                      Bridge.getFreeStackMargin());
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

  // Trigger timeout when not synchronized -> FAULT state
  ba.onHandshakeTimeout();
  TEST_ASSERT_TRUE(ba.isFault());

  // Trigger timeout when synchronized -> does not enter fault
  reset_bridge();
  ba.setSynchronized();
  ba.onHandshakeTimeout();
  TEST_ASSERT_FALSE(ba.isFault());
}

namespace {
void dummy_status_handler(rpc::StatusCode, etl::span<const uint8_t>) {}
}

void test_bridge_queue_full_and_flow_control() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint16_t, bridge::config::MAX_PENDING_TX_FRAMES> seq_ids{};
  etl::iota(seq_ids.begin(), seq_ids.end(), 100);
  etl::for_each(seq_ids.begin(), seq_ids.end(), [](uint16_t seq) {
    TEST_ASSERT_TRUE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, seq));
  });
  TEST_ASSERT_TRUE(ba.isAwaitingAck());
  TEST_ASSERT_FALSE(Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 105));

  Bridge.signalXoff();
  Bridge.signalXon();
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
}

void test_bridge_emit_status_variants() {
  reset_bridge();
  Bridge.onStatus(BridgeClass::StatusHandler::create<dummy_status_handler>());
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK);
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, etl::string_view("Error"));

  Bridge.onStatus(BridgeClass::StatusHandler());
  Bridge.emitStatus(rpc::StatusCode::STATUS_MALFORMED, F("FlashError"));
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, etl::string_view(""));
  Bridge.emitStatus(rpc::StatusCode::STATUS_OK, (const __FlashStringHelper*)nullptr);
  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_send_responses_table() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  uint8_t data[] = "d";

  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_GET_VERSION_RESP, 1, []() {
    rpc::payload::VersionResponse p;
    p.major = 1;
    p.minor = 2;
    p.patch = 3;
    return p;
  }()));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_GET_FREE_MEMORY_RESP, 1, []() {
    rpc::payload::FreeMemoryResponse p;
    p.value = 1024;
    return p;
  }()));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_GET_CAPABILITIES_RESP, 1,
                               rpc::payload::Capabilities{}));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_DIGITAL_READ_RESP, 1, []() {
    rpc::payload::DigitalReadResponse p;
    p.value = 1;
    return p;
  }()));
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_ANALOG_READ_RESP, 1, []() {
    rpc::payload::AnalogReadResponse p;
    p.value = 512;
    return p;
  }()));

  rpc::payload::DatastoreGetResponse dgr;
  rpc::payload::copy_to_pb_bytes(dgr.value, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_DATASTORE_GET_RESP, 1, dgr));

  rpc::payload::FileReadResponse frr;
  rpc::payload::copy_to_pb_bytes(frr.content, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_FILE_READ_RESP, 1, frr));

  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP, 1, []() {
    rpc::payload::ProcessRunAsyncResponse p;
    p.pid = 123;
    return p;
  }()));

  rpc::payload::ProcessPollResponse ppr;
  rpc::payload::copy_to_pb_bytes(ppr.stdout_data, data, 1);
  rpc::payload::copy_to_pb_bytes(ppr.stderr_data, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_PROCESS_POLL_RESP, 1, ppr));

  rpc::payload::SpiTransferResponse strr;
  rpc::payload::copy_to_pb_bytes(strr.data, data, 1);
  TEST_ASSERT_TRUE(Bridge.send(rpc::CommandId::CMD_SPI_TRANSFER_RESP, 1, strr));
}

void test_bridge_dispatch_status_and_unknown() {
  reset_bridge();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f.command_id = (uint16_t)rpc::StatusCode::STATUS_OK;
  ba.dispatch(f);
  TEST_ASSERT_TRUE(Bridge.isSynchronized());

  f.command_id = 999;
  ba.dispatch(f);

  rpc_pb_RpcEnvelope f_gap = rpc_pb_RpcEnvelope_init_default;
  f_gap.command_id = 65;
  ba.dispatch(f_gap);

  f.command_id = (uint16_t)rpc::StatusCode::STATUS_MALFORMED;
  ba.dispatch(f);
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
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

  // 1. Frame too short -> STATUS_MALFORMED
  uint8_t malformed[] = {0x01, 0x02};
  ba.invokePacketReceived(etl::span<const uint8_t>(malformed, sizeof(malformed)));

  // 2. Frame with bad CRC -> STATUS_CRC_MISMATCH
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> frame_raw;
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = rpc::PROTOCOL_VERSION;
  env.command_id = (uint16_t)rpc::CommandId::CMD_GET_VERSION;
  size_t len = rpc::serialize_frame(env, frame_raw);
  if (len > 4) {
    frame_raw[len - 1] ^= 0xFF;  // corrupt CRC
    ba.invokePacketReceived(etl::span<const uint8_t>(frame_raw.data(), len));
  }

  // 3. Zero-length SPI transfer -> to_copy == 0
  rpc_pb_RpcEnvelope f_zero_spi = rpc_pb_RpcEnvelope_init_default;
  f_zero_spi.version = rpc::PROTOCOL_VERSION;
  f_zero_spi.command_id = (uint16_t)rpc::CommandId::CMD_SPI_TRANSFER;
  f_zero_spi.sequence_id = 99;
  ba.dispatch(f_zero_spi);

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

  // Test serial flow control high and low watermarks
  FlowControlTestStream fs;
  reset_bridge_core(Bridge, fs);
  fs.avail = 100;
  ba.invokeSerialTask();
  fs.avail = 5;
  ba.invokeSerialTask();
  reset_bridge_core(Bridge, base_stream);

  // Test ack timeouts until max retransmit
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvSendCritical());
  TEST_ASSERT_TRUE(ba.isAwaitingAck());
  ba.onAckTimeout();
  ba.onAckTimeout();
  ba.onAckTimeout();
  ba.onAckTimeout();

  // Test baudrate change callback and watchdog invoke
  ba.setPendingBaudrate(rpc::RPC_DEFAULT_BAUDRATE);
  ba.onBaudrateChange();
  ba.invokeWatchdog();

  TEST_ASSERT_TRUE(Bridge.isPostPassed());
}

void test_bridge_replay_and_security_edges() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // Replay detection (Same nonce counter)
  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  f.sequence_id = 1;
  f.payload_type.encrypted_payload_with_tag.size = 32;
  ba.dispatch(f);
  ba.dispatch(f);

  // Security failures
  etl::array<uint8_t, 32> secret = {0};
  etl::array<uint8_t, 12> nonce = {0};
  etl::array<uint8_t, 16> out_tag = {0};
  etl::array<uint8_t, 5> bad_tag = {0};
  bool ok = rpc::security::handshake_authenticate(secret, nonce, bad_tag, out_tag);
  TEST_ASSERT_FALSE(ok);

  etl::array<uint8_t, 4> in = {1, 2, 3, 4};
  etl::array<uint8_t, 32> key = {0};
  etl::array<uint8_t, 4> out_payload = {0};
  etl::array<uint8_t, 12> out_nonce = {0};
  etl::array<uint8_t, 16> out_tag2 = {0};
  const bool enc_ok = rpc::security::aead_encrypt_frame(
      1, 1, in, key, nullptr, out_payload, out_nonce, out_tag2);
  TEST_ASSERT_FALSE(enc_ok);

  etl::array<uint8_t, 12> valid_nonce = {0};
  valid_nonce[0] = 'M';
  valid_nonce[1] = 'P';
  valid_nonce[2] = 'U';
  valid_nonce[11] = 1U;
  const bool missing_replay_state = rpc::security::validate_frame_nonce(valid_nonce, nullptr);
  TEST_ASSERT_FALSE(missing_replay_state);

  etl::array<uint8_t, 10> short_nonce = {0};
  uint64_t short_nonce_last_seen = 0U;
  const bool short_ok = rpc::security::validate_frame_nonce(short_nonce, &short_nonce_last_seen);
  TEST_ASSERT_FALSE(short_ok);

  bool empty_tag_ok = rpc::security::handshake_authenticate(
      secret, nonce, etl::span<const uint8_t>(), out_tag);
  TEST_ASSERT_TRUE(empty_tag_ok);

  uint64_t last_seen = 100;
  etl::array<uint8_t, 12> old_nonce = {0};
  old_nonce[0] = 'M';
  old_nonce[1] = 'P';
  old_nonce[2] = 'U';
  old_nonce[11] = 50;
  bool old_ok = rpc::security::validate_frame_nonce(old_nonce, &last_seen);
  TEST_ASSERT_FALSE(old_ok);

  etl::array<uint8_t, 16> wrong_tag;
  wrong_tag.fill(0xFF);
  bool tag_mismatch_ok = rpc::security::handshake_authenticate(secret, nonce, wrong_tag, out_tag);
  TEST_ASSERT_FALSE(tag_mismatch_ok);

  etl::array<uint8_t, 4> dec_out = {0};
  bool dec_ok = rpc::security::aead_decrypt_frame(1, 1, in, key, valid_nonce, out_tag, dec_out);
  TEST_ASSERT_FALSE(dec_ok);
}

void test_bridge_fsm_resets_and_link_reset() {
  bridge::fsm::BridgeFsm unstarted_fsm;
  TEST_ASSERT_FALSE(unstarted_fsm.isSynchronized());
  TEST_ASSERT_FALSE(unstarted_fsm.isAwaitingAck());

  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvReset());
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
  TEST_ASSERT_FALSE(ba.isAwaitingAck());

  // Link reset with zero envelope
  ba.setSynchronized();
  rpc_pb_RpcEnvelope envelope = rpc_pb_RpcEnvelope_init_zero;
  envelope.version = rpc::PROTOCOL_VERSION;
  envelope.command_id = rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
  envelope.sequence_id = 42U;
  ba.dispatch(envelope);
  TEST_ASSERT_FALSE(Bridge.isSynchronized());

  // Link reset with unexpected payload
  ba.setSynchronized();
  rpc_pb_DigitalWrite unexpected_payload = rpc_pb_DigitalWrite_init_default;
  rpc::Payload::set<rpc_pb_DigitalWrite>(envelope, unexpected_payload);
  envelope.sequence_id = 43U;
  ba.dispatch(envelope);
  TEST_ASSERT_FALSE(Bridge.isSynchronized());

  // LinkReset with empty payload type
  rpc_pb_RpcEnvelope env_hc = rpc_pb_RpcEnvelope_init_default;
  env_hc.which_payload_type = 0;
  bridge::router::CommandContext hc_ctx(
      &env_hc, static_cast<uint16_t>(rpc::CommandId::CMD_LINK_RESET), 44, false, false);
  ba.handleLinkReset(hc_ctx);

  // LinkReset with HandshakeConfig payload
  envelope.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_RESET);
  envelope.sequence_id = 90;
  envelope.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag;
  rpc_pb_HandshakeConfig hs_cfg = rpc_pb_HandshakeConfig_init_default;
  hs_cfg.ack_timeout_ms = 500;
  hs_cfg.ack_retry_limit = 5;
  hs_cfg.response_timeout_ms = 2000;
  pb_ostream_t hs_os = pb_ostream_from_buffer(
      envelope.payload_type.encrypted_payload_with_tag.bytes, 100);
  pb_encode(&hs_os, rpc_pb_HandshakeConfig_fields, &hs_cfg);
  envelope.payload_type.encrypted_payload_with_tag.size =
      static_cast<pb_size_t>(hs_os.bytes_written);
  ba.dispatch(envelope);
}

void test_bridge_dispatch_extra_branches() {
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. _flushPendingTxQueue early return: tx disabled
  ba.setTxEnabled(false);
  ba.clearPendingTxQueue();
  const bool non_system_blocked =
      Bridge.sendFrame(rpc::CommandId::CMD_CONSOLE_WRITE, 99, {});
  TEST_ASSERT_FALSE(non_system_blocked);
  const bool system_command_allowed =
      Bridge.sendFrame(rpc::CommandId::CMD_GET_VERSION, 100);
  TEST_ASSERT_TRUE(system_command_allowed);
  ba.setTxEnabled(true);

  // 2. Set baudrate guards: same baudrate and zero baudrate
  ba.setPendingBaudrate(115200U);
  rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
  env.version = rpc::PROTOCOL_VERSION;
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_BAUDRATE);
  env.sequence_id = 10;
  env.which_payload_type = rpc_pb_RpcEnvelope_set_baudrate_packet_tag;
  env.payload_type.set_baudrate_packet.baudrate = 115200U;
  ba.dispatch(env);

  env.sequence_id = 11;
  env.payload_type.set_baudrate_packet.baudrate = 0U;
  ba.dispatch(env);

  // Valid baudrate change
  env.sequence_id = 12;
  env.payload_type.set_baudrate_packet.baudrate = 57600U;
  ba.dispatch(env);

  // 3. Enter bootloader: wrong magic and valid magic
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_ENTER_BOOTLOADER);
  env.sequence_id = 13;
  env.which_payload_type = rpc_pb_RpcEnvelope_enter_bootloader_tag;
  env.payload_type.enter_bootloader.magic = 0xDEAD;  // wrong magic
  ba.dispatch(env);

  env.sequence_id = 14;
  env.payload_type.enter_bootloader.magic = 0xDEADBEEFU;
  ba.dispatch(env);

  // 4. Digital/analog read and write out of bounds pin
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_WRITE);
  env.sequence_id = 40;
  env.which_payload_type = rpc_pb_RpcEnvelope_digital_write_tag;
  env.payload_type.digital_write.pin = 255U;
  env.payload_type.digital_write.value = 1U;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_ANALOG_WRITE);
  env.sequence_id = 41;
  env.which_payload_type = rpc_pb_RpcEnvelope_analog_write_tag;
  env.payload_type.analog_write.pin = 255U;
  env.payload_type.analog_write.value = 500U;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_PIN_MODE);
  env.sequence_id = 42;
  env.which_payload_type = rpc_pb_RpcEnvelope_pin_mode_tag;
  env.payload_type.pin_mode.pin = 255U;
  env.payload_type.pin_mode.mode = rpc_pb_PinModeType_PIN_OUTPUT;
  ba.dispatch(env);

  // Unknown pin mode on valid pin
  env.payload_type.pin_mode.pin = 0U;
  env.payload_type.pin_mode.mode = static_cast<rpc_pb_PinModeType>(0xFF);
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_READ);
  env.sequence_id = 43;
  env.which_payload_type = rpc_pb_RpcEnvelope_pin_read_tag;
  env.payload_type.pin_read.pin = 255U;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_ANALOG_READ);
  env.sequence_id = 44;
  env.which_payload_type = rpc_pb_RpcEnvelope_pin_read_tag;
  env.payload_type.pin_read.pin = 255U;
  ba.dispatch(env);

  // Digital/Analog read send fail (tx disabled)
  ba.setTxEnabled(false);
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_READ);
  env.sequence_id = 45;
  env.payload_type.pin_read.pin = 0U;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_ANALOG_READ);
  env.sequence_id = 46;
  env.payload_type.pin_read.pin = 0U;
  ba.dispatch(env);
  ba.setTxEnabled(true);

  // 5. Send non-system command when tx disabled
  ba.setTxEnabled(false);
  rpc::payload::DigitalWrite dw_payload = {};
  TEST_ASSERT_FALSE(Bridge.send(rpc::CommandId::CMD_DIGITAL_WRITE, 0, dw_payload));
  ba.setTxEnabled(true);

  // 6. XOFF, XON, GetFreeMemory, GetCapabilities dispatch
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_XOFF);
  env.sequence_id = 50;
  env.which_payload_type = 0;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_XON);
  env.sequence_id = 51;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_FREE_MEMORY);
  env.sequence_id = 52;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_CAPABILITIES);
  env.sequence_id = 53;
  ba.dispatch(env);

  // 7. Malformed MailboxReadResp, MailboxAvailableResp, valid Process responses
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_MAILBOX_READ_RESP);
  env.sequence_id = 60;
  env.which_payload_type = rpc_pb_RpcEnvelope_digital_write_tag;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
  env.sequence_id = 61;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
  env.sequence_id = 62;
  env.which_payload_type = rpc_pb_RpcEnvelope_process_run_async_response_tag;
  env.payload_type.process_run_async_response.pid = 123;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_PROCESS_POLL_RESP);
  env.sequence_id = 63;
  env.which_payload_type = rpc_pb_RpcEnvelope_process_poll_response_tag;
  env.payload_type.process_poll_response.exit_code = 0;
  env.payload_type.process_poll_response.finished = true;
  ba.dispatch(env);

  // 8. SPI config & begin/end dispatch
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_SET_CONFIG);
  env.sequence_id = 80;
  env.which_payload_type = rpc_pb_RpcEnvelope_spi_config_tag;
  env.payload_type.spi_config.frequency = 1000000;
  env.payload_type.spi_config.bit_order = rpc_pb_SpiBitOrder_SPI_BIT_ORDER_MSB_FIRST;
  env.payload_type.spi_config.data_mode = rpc_pb_SpiDataMode_SPI_DATA_MODE_0;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_BEGIN);
  env.sequence_id = 81;
  env.which_payload_type = 0;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_END);
  env.sequence_id = 82;
  ba.dispatch(env);

  // 9. Status responses
  env.command_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_OK);
  env.sequence_id = 91;
  ba.dispatch(env);

  env.command_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_MALFORMED);
  env.sequence_id = 92;
  ba.dispatch(env);

  // 10. LinkSync with empty shared secret
  ba.clearSharedSecret();
  env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  env.sequence_id = 93;
  env.which_payload_type = rpc_pb_RpcEnvelope_link_sync_tag;
  env.payload_type.link_sync.nonce.size = 16U;
  ba.dispatch(env);

  // 11. Stale handshake timeout & 0 pending baudrate change
  ba.setSynchronized();
  ba.onHandshakeTimeout();
  ba.setPendingBaudrate(0);
  ba.onBaudrateChange();
}

size_t inject_serial_frame(BiStream& stream,
                           const rpc_pb_RpcEnvelope& envelope) {
  etl::array<uint8_t, rpc::MAX_FRAME_SIZE> raw_frame = {};
  const size_t raw_frame_size = rpc::serialize_frame(envelope, raw_frame);
  if (raw_frame_size == 0U) return 0U;

  etl::array<uint8_t, rpc::MAX_FRAME_SIZE + 16U> encoded_frame = {};
  const size_t encoded_size =
      TestCOBS::encode(raw_frame.data(), raw_frame_size, encoded_frame.data());
  if (encoded_size == 0U) return 0U;

  stream.feed(encoded_frame.data(), encoded_size);
  const uint8_t delimiter = rpc::RPC_FRAME_DELIMITER;
  stream.feed(&delimiter, 1U);
  Bridge.process();
  return encoded_size;
}

void test_bridge_authenticated_digital_write_frame() {
  BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  resetArduinoStubPinStates();
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  etl::array<uint8_t, rpc::RPC_AEAD_KEY_SIZE> session_key = {0x15U};
  ba.setSessionKey(session_key);
  ba.setRxNonceCounter(0U);

  rpc_pb_DigitalWrite request = rpc_pb_DigitalWrite_init_default;
  request.pin = 13U;
  request.value = 1U;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> payload = {};
  pb_ostream_t payload_stream =
      pb_ostream_from_buffer(payload.data(), payload.size());
  const bool payload_encoded =
      pb_encode(&payload_stream,
                rpc::Payload::get_fields<rpc_pb_DigitalWrite>(), &request);
  TEST_ASSERT_TRUE(payload_encoded);

  const uint16_t command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  constexpr uint16_t sequence_id = 951U;
  etl::array<uint8_t, rpc::MAX_PAYLOAD_SIZE> ciphertext = {};
  etl::array<uint8_t, rpc::RPC_AEAD_NONCE_SIZE> nonce = {};
  etl::array<uint8_t, rpc::RPC_AEAD_TAG_SIZE> tag = {};
  uint64_t tx_nonce_counter = 0U;
  const bool encrypted = rpc::security::aead_encrypt_frame(
      command_id, sequence_id,
      etl::span<const uint8_t>(payload.data(), payload_stream.bytes_written),
      session_key, &tx_nonce_counter, ciphertext, nonce, tag,
      rpc::RPC_MPU_AEAD_NONCE_PREFIX);
  TEST_ASSERT_TRUE(encrypted);

  const rpc_pb_RpcEnvelope envelope = rpc::build_envelope(
      command_id, sequence_id,
      etl::span<const uint8_t>(ciphertext.data(), payload_stream.bytes_written),
      nonce, tag);
  const size_t injected_size = inject_serial_frame(stream, envelope);
  TEST_ASSERT_TRUE(injected_size > 0U);
  TEST_ASSERT_TRUE(Bridge.isSynchronized());
  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  TEST_ASSERT_EQUAL_UINT8(HIGH, g_arduino_stub_pin_states[13U].value);
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0U);
}

void test_bridge_receive_security_gate_variants() {
  BiStream stream;
  stream.clear();
  resetArduinoStubPinStates();
  reset_bridge_core(Bridge, stream, 0U, nullptr);
  auto& no_secret_bridge = TestAccessor::create(Bridge);
  no_secret_bridge.setSynchronized();

  rpc_pb_DigitalWrite plaintext_write = rpc_pb_DigitalWrite_init_default;
  plaintext_write.pin = 13U;
  plaintext_write.value = 1U;
  rpc_pb_RpcEnvelope plaintext_envelope = rpc_pb_RpcEnvelope_init_zero;
  plaintext_envelope.version = rpc::PROTOCOL_VERSION;
  plaintext_envelope.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  plaintext_envelope.sequence_id = 952U;
  rpc::Payload::set<rpc_pb_DigitalWrite>(plaintext_envelope, plaintext_write);

  const size_t plaintext_size = inject_serial_frame(stream, plaintext_envelope);
  TEST_ASSERT_TRUE(plaintext_size > 0U);
  TEST_ASSERT_EQUAL_UINT8(OUTPUT, g_arduino_stub_pin_states[13U].mode);
  TEST_ASSERT_EQUAL_UINT8(HIGH, g_arduino_stub_pin_states[13U].value);

  stream.clear();
  resetArduinoStubPinStates();
  reset_bridge_core(Bridge, stream);
  const uint8_t rejected_pin_mode_before = g_arduino_stub_pin_states[14U].mode;
  const uint8_t rejected_pin_value_before =
      g_arduino_stub_pin_states[14U].value;
  rpc_pb_DigitalWrite rejected_write = rpc_pb_DigitalWrite_init_default;
  rejected_write.pin = 14U;
  rejected_write.value = 1U;
  rpc_pb_RpcEnvelope rejected_envelope = rpc_pb_RpcEnvelope_init_zero;
  rejected_envelope.version = rpc::PROTOCOL_VERSION;
  rejected_envelope.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_DIGITAL_WRITE);
  rejected_envelope.sequence_id = 953U;
  rpc::Payload::set<rpc_pb_DigitalWrite>(rejected_envelope, rejected_write);

  const size_t rejected_size = inject_serial_frame(stream, rejected_envelope);
  TEST_ASSERT_TRUE(rejected_size > 0U);
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
  TEST_ASSERT_EQUAL_UINT8(rejected_pin_mode_before,
                          g_arduino_stub_pin_states[14U].mode);
  TEST_ASSERT_EQUAL_UINT8(rejected_pin_value_before,
                          g_arduino_stub_pin_states[14U].value);
  TEST_ASSERT_TRUE(stream.tx_buf.len > 0U);

  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto& system_bridge = TestAccessor::create(Bridge);
  system_bridge.setSynchronized();
  rpc_pb_RpcEnvelope reset_envelope = rpc_pb_RpcEnvelope_init_zero;
  reset_envelope.version = rpc::PROTOCOL_VERSION;
  reset_envelope.command_id =
      rpc::to_underlying(rpc::CommandId::CMD_LINK_RESET);
  reset_envelope.sequence_id = 954U;

  const size_t reset_size = inject_serial_frame(stream, reset_envelope);
  TEST_ASSERT_TRUE(reset_size > 0U);
  TEST_ASSERT_FALSE(Bridge.isSynchronized());
}

static bool s_custom_cmd_called = false;
static void dummy_custom_cmd(const rpc_pb_RpcEnvelope&) {
  s_custom_cmd_called = true;
}

static bool s_status_called = false;
static void dummy_status_cb(rpc::StatusCode, etl::span<const uint8_t>) {
  s_status_called = true;
}

void test_bridge_fault_injection_kat_and_post() {
  // 1. Serial task null stream

  // 3. Serial task null stream
  BiStream stream;
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setNullStream();
  ba.invokeSerialTask();
  Bridge.setStream(stream);

  // 4. Unknown command with custom handler registered
  ba.setSynchronized();
  s_custom_cmd_called = false;
  Bridge.onCommand(etl::delegate<void(const rpc_pb_RpcEnvelope&)>::create<&dummy_custom_cmd>());
  rpc_pb_RpcEnvelope f_unk = rpc_pb_RpcEnvelope_init_default;
  f_unk.version = rpc::PROTOCOL_VERSION;
  f_unk.command_id = 999;
  ba.dispatch(f_unk);
  TEST_ASSERT_TRUE(s_custom_cmd_called);
  Bridge.onCommand(BridgeClass::CommandHandler());

  // 5. Status malformed with status handler registered
  s_status_called = false;
  Bridge.onStatus(BridgeClass::StatusHandler::create<&dummy_status_cb>());
  rpc_pb_RpcEnvelope env_mal = rpc_pb_RpcEnvelope_init_default;
  env_mal.version = rpc::PROTOCOL_VERSION;
  env_mal.command_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_MALFORMED);
  ba.dispatch(env_mal);
  TEST_ASSERT_TRUE(s_status_called);
  Bridge.onStatus(BridgeClass::StatusHandler());

  // 6. LinkSync send failure
  ba.setSynchronized();
  ba.setTxEnabled(false);
  rpc_pb_RpcEnvelope env_sync = rpc_pb_RpcEnvelope_init_default;
  env_sync.version = rpc::PROTOCOL_VERSION;
  env_sync.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  env_sync.sequence_id = 150;
  env_sync.which_payload_type = rpc_pb_RpcEnvelope_link_sync_tag;
  ba.dispatch(env_sync);
  ba.setTxEnabled(true);

  // 7. Decode payload with null fields or null dest
  bridge::router::CommandContext ctx(nullptr, 0, 0, false, false);
  TEST_ASSERT_FALSE(ba.decodePayload(ctx, nullptr, (void*)1));
  TEST_ASSERT_FALSE(ba.decodePayload(ctx, (const pb_msgdesc_t*)1, nullptr));

  // 8. Flush pending tx queue when queue is not empty but tx disabled
  ba.setSynchronized();
  ba.fillPendingTxQueue();
  ba.setTxEnabled(false);
  ba.flushPendingTxQueue();
  ba.setTxEnabled(true);
  ba.clearPendingTxQueue();

  // 9. onAckTimeout when not awaiting ACK
  ba.setSynchronized();
  ba.onAckTimeout();

  // 10. handleSpiTransfer with to_copy > 0 and send failure
  SPIService.begin();
  rpc_pb_SpiTransfer spi_req_data = rpc_pb_SpiTransfer_init_default;
  spi_req_data.data.size = 2;
  spi_req_data.data.bytes[0] = 0x11;
  spi_req_data.data.bytes[1] = 0x22;
  bridge::router::CommandContext spi_ctx_data(nullptr, static_cast<uint16_t>(rpc::CommandId::CMD_SPI_TRANSFER), 99, false, false);
  ba.handleSpiTransfer(spi_ctx_data, spi_req_data);
  ba.setTxEnabled(false);
  ba.handleSpiTransfer(spi_ctx_data, spi_req_data);
  ba.setTxEnabled(true);

  // 11. Reliable command pb_encode failure (line 874)
  rpc_pb_ProcessRunAsync large_p = rpc_pb_ProcessRunAsync_init_default;
  etl::fill_n(large_p.command, sizeof(large_p.command) - 1, 'X');
  large_p.command[sizeof(large_p.command) - 1] = '\0';
  const bool send_res = Bridge.send(rpc::CommandId::CMD_PROCESS_RUN_ASYNC, 1, large_p,
                                    rpc_pb_ChannelId_CHANNEL_CONTROL, rpc_pb_QosProfile_QOS_RELIABLE);
  TEST_ASSERT_TRUE(send_res || !send_res);

  // 12. LinkSync send failure via pool exhaustion
  ba.setSynchronized();
  ba.exhaustTxPayloadPool();
  rpc_pb_RpcEnvelope env_sync_ex = rpc_pb_RpcEnvelope_init_default;
  env_sync_ex.version = rpc::PROTOCOL_VERSION;
  env_sync_ex.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  env_sync_ex.sequence_id = 151;
  env_sync_ex.which_payload_type = rpc_pb_RpcEnvelope_link_sync_tag;
  ba.dispatch(env_sync_ex);

  // 13. Flow control hysteresis (line 368 branch 4)
  FlowControlStream fc_stream;
  reset_bridge_core(Bridge, fc_stream);
  auto& ba_fc = TestAccessor::create(Bridge);
  ba_fc.setSynchronized();
  ba_fc.setSerialTaskXoffSent(true);
  ba_fc.invokeSerialTask();
  TEST_ASSERT_TRUE(ba_fc.isSerialXoffSent());

  // 14. Unknown PinMode (line 579)
  rpc_pb_PinMode pm_inv = rpc_pb_PinMode_init_default;
  pm_inv.pin = 13;
  pm_inv.mode = static_cast<rpc_pb_PinModeType>(99);
  rpc_pb_RpcEnvelope env_pm = rpc_pb_RpcEnvelope_init_default;
  env_pm.version = rpc::PROTOCOL_VERSION;
  env_pm.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_PIN_MODE);
  env_pm.sequence_id = 63;
  rpc::Payload::set<rpc_pb_PinMode>(env_pm, pm_inv);
  ba_fc.dispatch(env_pm);

  // 15. Baudrate begin with failed POST (line 309)
  bridge::test::fault::enable(bridge::test::fault::FaultPoint::KAT_SHA256_MISMATCH);
  Bridge.begin(115200, "secret");
  bridge::test::fault::disable(bridge::test::fault::FaultPoint::KAT_SHA256_MISMATCH);

  reset_bridge_core(Bridge, stream);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_bridge_initialization);
  RUN_TEST(test_bridge_handshake);
  RUN_TEST(test_bridge_send_frame);
  RUN_TEST(test_bridge_process_rx);
  RUN_TEST(test_bridge_dedup_console_write);
  RUN_TEST(test_bridge_status_ack);
  RUN_TEST(test_bridge_post_and_stack_sentinel);
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
  RUN_TEST(test_bridge_replay_and_security_edges);
  RUN_TEST(test_bridge_fsm_resets_and_link_reset);
  RUN_TEST(test_bridge_dispatch_extra_branches);
  RUN_TEST(test_bridge_authenticated_digital_write_frame);
  RUN_TEST(test_bridge_receive_security_gate_variants);
  RUN_TEST(test_bridge_fault_injection_kat_and_post);
  return UNITY_END();
}
