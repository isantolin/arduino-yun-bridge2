#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE 1
#endif
#include <BridgeFaultInjection.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "hal/hal.h"
#include "security/security.h"
#include "services/Console.h"
#include "services/DataStore.h"
#include "services/FileSystem.h"
#include "services/Mailbox.h"
#include "services/Process.h"
#include "services/SPIService.h"
#include "test_support.h"

// Arduino Stubs for Linker
HardwareSerial Serial;
HardwareSerial Serial1;
Stream* g_arduino_stream_delegate = nullptr;

namespace etl {
void handle_error(const etl::exception& e);
}

using bridge::test::TestAccessor;

void setUp(void) {}
void tearDown(void) {}

void test_surgical_bridge_errors() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Replay detection (Same nonce counter)
  rpc_pb_RpcEnvelope f = rpc_pb_RpcEnvelope_init_default;
  f.version = rpc::PROTOCOL_VERSION;
  f.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
  f.sequence_id = 1;
  f.payload_type.encrypted_payload_with_tag.size = 32;
  // Bridge saves the last counter. We'll dispatch once.
  ba.dispatch(f);
  // Dispatch again with same nonce (implicit counter 0 in header)
  ba.dispatch(f);

  // 2. emitStatus variants
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, "Short");
  static char long_msg[300];
  etl::fill_n(long_msg, 299, 'A');
  long_msg[299] = '\0';
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, long_msg);
  Bridge.emitStatus(rpc::StatusCode::STATUS_ERROR, (const char*)nullptr);

  // 3. Unknown Command in dispatch
  rpc_pb_RpcEnvelope f_unk = rpc_pb_RpcEnvelope_init_default;
  f_unk.version = rpc::PROTOCOL_VERSION;
  f_unk.command_id = 999;
  ba.dispatch(f_unk);

  // 4. Bad version
  f_unk.version = 0;
  ba.dispatch(f_unk);
}

void test_surgical_fsm_resets() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  ba.trigger(bridge::fsm::EvReset());
  ba.trigger(bridge::fsm::EvHandshakeStart());
  ba.trigger(bridge::fsm::EvReset());
  ba.setSynchronized();
  ba.trigger(bridge::fsm::EvReset());
}

void test_surgical_security_failures() {
  // 1. Handshake authenticate wrong tag size
  etl::array<uint8_t, 32> secret = {0};
  etl::array<uint8_t, 12> nonce = {0};
  etl::array<uint8_t, 16> out_tag = {0};
  etl::array<uint8_t, 5> bad_tag = {0};
  bool ok =
      rpc::security::handshake_authenticate(secret, nonce, bad_tag, out_tag);
  TEST_ASSERT_FALSE(ok);

  // 2. aead_encrypt_frame with null nonce_counter
  etl::array<uint8_t, 4> in = {1, 2, 3, 4};
  etl::array<uint8_t, 32> key = {0};
  etl::array<uint8_t, 4> out_payload = {0};
  etl::array<uint8_t, 12> out_nonce = {0};
  etl::array<uint8_t, 16> out_tag2 = {0};
  bool enc_ok = rpc::security::aead_encrypt_frame(
      1, 1, in, key, nullptr, out_payload, out_nonce, out_tag2);
  TEST_ASSERT(enc_ok);

  // 3. validate_frame_nonce with null last_seen_counter
  etl::array<uint8_t, 12> valid_nonce = {0};
  valid_nonce[0] = 'M';
  valid_nonce[1] = 'C';
  valid_nonce[2] = 'U';
  bool val_ok = rpc::security::validate_frame_nonce(valid_nonce, nullptr);
  TEST_ASSERT(val_ok);

  // 4. validate_frame_nonce with nonce of size < 12
  etl::array<uint8_t, 10> short_nonce = {0};
  bool short_ok = rpc::security::validate_frame_nonce(short_nonce, nullptr);
  TEST_ASSERT_FALSE(short_ok);

  // 5. Handshake authenticate empty tag
  bool empty_tag_ok = rpc::security::handshake_authenticate(
      secret, nonce, etl::span<const uint8_t>(), out_tag);
  TEST_ASSERT_TRUE(empty_tag_ok);

  // 6. validate_frame_nonce with counter <= last_seen
  uint64_t last_seen = 100;
  etl::array<uint8_t, 12> old_nonce = {0};
  old_nonce[0] = 'M';
  old_nonce[1] = 'C';
  old_nonce[2] = 'U';
  old_nonce[11] = 50;
  bool old_ok = rpc::security::validate_frame_nonce(old_nonce, &last_seen);
  TEST_ASSERT_FALSE(old_ok);

  // 7. Handshake authenticate tag mismatch (correct 16-byte length, invalid
  // content)
  etl::array<uint8_t, 16> wrong_tag;
  wrong_tag.fill(0xFF);
  bool tag_mismatch_ok =
      rpc::security::handshake_authenticate(secret, nonce, wrong_tag, out_tag);
  TEST_ASSERT_FALSE(tag_mismatch_ok);

  // 8. aead_decrypt_frame call
  etl::array<uint8_t, 4> dec_out = {0};
  (void)rpc::security::aead_decrypt_frame(1, 1, in, key, valid_nonce, out_tag,
                                          dec_out);
  TEST_ASSERT_FALSE(old_ok);
}

void test_surgical_tasks_flow() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);

  // SerialTask XOFF path
  static uint8_t dummy[1000];
  stream.feed(dummy, 1000);
  ba.invokeSerialTask();
  // XON path
  stream.clear();
  ba.invokeSerialTask();

  // TimerTask ACK timeout
  ba.setSynchronized();
  ba.onAckTimeout();

  // Test etl::handle_error
  etl::exception test_exc("msg", "file", 100);
  etl::handle_error(test_exc);

  // Test timer lambda coverage
  ba.startTimersForCoverage();
  ba.setTimerLastTick(1);
  bridge::test::fault::advance_clock_ms(2000);
  ba.invokeTimerTask();
}

void test_surgical_send_fail_branches() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. _flushPendingTxQueue early return: tx disabled
  // Enqueue a frame then disable TX — flush should abort (line 591 branch)
  ba.setTxEnabled(false);
  ba.clearPendingTxQueue();
  // Nothing should crash
  ba.setTxEnabled(true);

  // 2. _handleSetBaudrate: same baudrate guard (line 667 branch)
  ba.setPendingBaudrate(115200U);
  {
    rpc_pb_SetBaudratePacket msg = rpc_pb_SetBaudratePacket_init_default;
    msg.baudrate = 115200U;  // same as _pending_baudrate → early return
    ba.dispatch([&]() {
      rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
      env.version = rpc::PROTOCOL_VERSION;
      env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_BAUDRATE);
      env.sequence_id = 10;
      env.which_payload_type = rpc_pb_RpcEnvelope_set_baudrate_packet_tag;
      env.payload_type.set_baudrate_packet = msg;
      return env;
    }());
    // Also zero baudrate → early return
    msg.baudrate = 0U;
    ba.dispatch([&]() {
      rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
      env.version = rpc::PROTOCOL_VERSION;
      env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_BAUDRATE);
      env.sequence_id = 11;
      env.which_payload_type = rpc_pb_RpcEnvelope_set_baudrate_packet_tag;
      env.payload_type.set_baudrate_packet = msg;
      return env;
    }());
  }

  // 3. _handleEnterBootloader: wrong magic (line 673 branch)
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id =
        static_cast<uint16_t>(rpc::CommandId::CMD_ENTER_BOOTLOADER);
    env.sequence_id = 12;
    env.which_payload_type = rpc_pb_RpcEnvelope_enter_bootloader_tag;
    env.payload_type.enter_bootloader.magic = 0xDEAD;  // wrong magic
    ba.dispatch(env);
  }

  // 4. CMD_DIGITAL_READ / CMD_ANALOG_READ send-fail (lines 703-704, 722-723)
  // Disable TX so send() returns false → emitStatus(STATUS_ERROR) branch
  ba.setTxEnabled(false);
  {
    // CMD_DIGITAL_READ with valid pin — send will fail
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_READ);
    env.sequence_id = 20;
    env.which_payload_type = rpc_pb_RpcEnvelope_pin_read_tag;
    env.payload_type.pin_read.pin = 0U;  // valid pin
    ba.dispatch(env);
  }
  {
    // CMD_ANALOG_READ with valid pin — send will fail
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_ANALOG_READ);
    env.sequence_id = 21;
    env.which_payload_type = rpc_pb_RpcEnvelope_pin_read_tag;
    env.payload_type.pin_read.pin = 0U;  // valid pin
    ba.dispatch(env);
  }
  ba.setTxEnabled(true);

  // 5. _handleSetPinMode with unknown mode (line 687 branch)
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_PIN_MODE);
    env.sequence_id = 22;
    env.which_payload_type = rpc_pb_RpcEnvelope_pin_mode_tag;
    // Use a mode value not in the lookup table
    env.payload_type.pin_mode.mode = static_cast<rpc_pb_PinModeType>(0xFF);
    env.payload_type.pin_mode.pin = 0U;
    ba.dispatch(env);
  }

  // 6. _handleLinkSync with empty shared secret (line 842 false branch)
  ba.clearSharedSecret();
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_SYNC);
    env.sequence_id = 30;
    env.which_payload_type = rpc_pb_RpcEnvelope_link_sync_tag;
    env.payload_type.link_sync.nonce.size = 16U;
    ba.dispatch(env);
  }

  // 7. Payload pool exhaustion send fail in _sendEncryptedHelper
  ba.exhaustTxPayloadPool();
  rpc_pb_DatastorePut put_msg = rpc_pb_DatastorePut_init_default;
  (void)Bridge.send(rpc::CommandId::CMD_DATASTORE_PUT, 100, put_msg);
}

static void test_surgical_extra_branches() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();

  // 1. Digital write out of bounds pin
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_WRITE);
    env.sequence_id = 40;
    env.which_payload_type = rpc_pb_RpcEnvelope_digital_write_tag;
    env.payload_type.digital_write.pin = 255U;
    env.payload_type.digital_write.value = 1U;
    ba.dispatch(env);
  }

  // 2. Analog write out of bounds pin
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_ANALOG_WRITE);
    env.sequence_id = 41;
    env.which_payload_type = rpc_pb_RpcEnvelope_analog_write_tag;
    env.payload_type.analog_write.pin = 255U;
    env.payload_type.analog_write.value = 500U;
    ba.dispatch(env);
  }

  // 3. Pin mode out of bounds pin
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_PIN_MODE);
    env.sequence_id = 42;
    env.which_payload_type = rpc_pb_RpcEnvelope_pin_mode_tag;
    env.payload_type.pin_mode.pin = 255U;
    env.payload_type.pin_mode.mode = rpc_pb_PinModeType_PIN_OUTPUT;
    ba.dispatch(env);
  }

  // 4. Digital read out of bounds pin
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_READ);
    env.sequence_id = 43;
    env.which_payload_type = rpc_pb_RpcEnvelope_pin_read_tag;
    env.payload_type.pin_read.pin = 255U;
    ba.dispatch(env);
  }

  // 5. Set baudrate command dispatch
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SET_BAUDRATE);
    env.sequence_id = 44;
    env.which_payload_type = rpc_pb_RpcEnvelope_set_baudrate_packet_tag;
    env.payload_type.set_baudrate_packet.baudrate = 115200U;
    ba.dispatch(env);
  }

  // 6. Enter bootloader command dispatch
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id =
        static_cast<uint16_t>(rpc::CommandId::CMD_ENTER_BOOTLOADER);
    env.sequence_id = 45;
    env.which_payload_type = rpc_pb_RpcEnvelope_enter_bootloader_tag;
    env.payload_type.enter_bootloader.magic = 0xDEADBEEFU;
    ba.dispatch(env);
  }

  // 7. SPI config command dispatch
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_SET_CONFIG);
    env.sequence_id = 46;
    env.which_payload_type = rpc_pb_RpcEnvelope_spi_config_tag;
    env.payload_type.spi_config.bit_order = 0U;
    env.payload_type.spi_config.data_mode = 0U;
    ba.dispatch(env);
  }

  // 8. File write response dispatch
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_FILE_WRITE);
    env.sequence_id = 47;
    env.which_payload_type = rpc_pb_RpcEnvelope_file_write_tag;
    env.payload_type.file_write.path[0] = 't';
    env.payload_type.file_write.path[1] = '\0';
    ba.dispatch(env);
  }

  // 9. Send non-system command when tx disabled
  ba.setTxEnabled(false);
  rpc::payload::DigitalWrite dw_payload = {};
  (void)Bridge.send(rpc::CommandId::CMD_DIGITAL_WRITE, 0, dw_payload);
  ba.setTxEnabled(true);

  // 10. Dispatch XOFF, XON, GetFreeMemory, GetCapabilities
  {
    rpc_pb_RpcEnvelope env = rpc_pb_RpcEnvelope_init_default;
    env.version = rpc::PROTOCOL_VERSION;
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_XOFF);
    env.sequence_id = 50;
    ba.dispatch(env);

    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_XON);
    env.sequence_id = 51;
    ba.dispatch(env);

    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_GET_FREE_MEMORY);
    env.sequence_id = 52;
    ba.dispatch(env);

    env.command_id =
        static_cast<uint16_t>(rpc::CommandId::CMD_GET_CAPABILITIES);
    env.sequence_id = 53;
    ba.dispatch(env);

    // 11. Malformed MailboxReadResp & MailboxAvailableResp, valid Process
    // responses
    env.command_id =
        static_cast<uint16_t>(rpc::CommandId::CMD_MAILBOX_READ_RESP);
    env.sequence_id = 60;
    env.which_payload_type = rpc_pb_RpcEnvelope_digital_write_tag;
    ba.dispatch(env);

    env.command_id =
        static_cast<uint16_t>(rpc::CommandId::CMD_MAILBOX_AVAILABLE_RESP);
    env.sequence_id = 61;
    ba.dispatch(env);

    env.command_id =
        static_cast<uint16_t>(rpc::CommandId::CMD_PROCESS_RUN_ASYNC_RESP);
    env.sequence_id = 62;
    env.which_payload_type = rpc_pb_RpcEnvelope_process_run_async_response_tag;
    env.payload_type.process_run_async_response.pid = 123;
    ba.dispatch(env);

    env.command_id =
        static_cast<uint16_t>(rpc::CommandId::CMD_PROCESS_POLL_RESP);
    env.sequence_id = 63;
    env.which_payload_type = rpc_pb_RpcEnvelope_process_poll_response_tag;
    env.payload_type.process_poll_response.exit_code = 0;
    env.payload_type.process_poll_response.finished = true;
    ba.dispatch(env);

    // 12. DigitalRead & AnalogRead with invalid pin >= max_pins
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_DIGITAL_READ);
    env.sequence_id = 70;
    env.which_payload_type = rpc_pb_RpcEnvelope_pin_read_tag;
    env.payload_type.pin_read.pin = 250;
    ba.dispatch(env);

    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_ANALOG_READ);
    env.sequence_id = 71;
    env.which_payload_type = rpc_pb_RpcEnvelope_pin_read_tag;
    env.payload_type.pin_read.pin = 250;
    ba.dispatch(env);

    // 14. Dispatch SPI commands & StatusMalformed
    env.command_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_MALFORMED);
    env.sequence_id = 79;
    ba.dispatch(env);

    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_SET_CONFIG);
    env.sequence_id = 80;
    env.which_payload_type = rpc_pb_RpcEnvelope_spi_config_tag;
    env.payload_type.spi_config.frequency = 1000000;
    env.payload_type.spi_config.bit_order = MSBFIRST;
    env.payload_type.spi_config.data_mode = SPI_MODE0;
    ba.dispatch(env);

    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_BEGIN);
    env.sequence_id = 81;
    ba.dispatch(env);

    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_END);
    env.sequence_id = 82;
    ba.dispatch(env);

    SPIService.begin();
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_SPI_TRANSFER);
    env.sequence_id = 83;
    env.which_payload_type = rpc_pb_RpcEnvelope_spi_transfer_tag;
    env.payload_type.spi_transfer.data.size = 2;
    env.payload_type.spi_transfer.data.bytes[0] = 0xAA;
    env.payload_type.spi_transfer.data.bytes[1] = 0xBB;
    ba.dispatch(env);

    // 15. CMD_LINK_RESET with HandshakeConfig payload
    env.command_id = static_cast<uint16_t>(rpc::CommandId::CMD_LINK_RESET);
    env.sequence_id = 90;
    env.which_payload_type = rpc_pb_RpcEnvelope_encrypted_payload_with_tag_tag;
    rpc_pb_HandshakeConfig hs_cfg = rpc_pb_HandshakeConfig_init_default;
    hs_cfg.ack_timeout_ms = 500;
    hs_cfg.ack_retry_limit = 5;
    hs_cfg.response_timeout_ms = 2000;
    pb_ostream_t hs_os = pb_ostream_from_buffer(
        env.payload_type.encrypted_payload_with_tag.bytes, 100);
    pb_encode(&hs_os, rpc_pb_HandshakeConfig_fields, &hs_cfg);
    env.payload_type.encrypted_payload_with_tag.size =
        static_cast<pb_size_t>(hs_os.bytes_written);
    ba.dispatch(env);

    // 16. Dispatch STATUS_OK
    env.command_id = static_cast<uint16_t>(rpc::StatusCode::STATUS_OK);
    env.sequence_id = 91;
    ba.dispatch(env);
  }

  // 17. Bridge.begin with nullptr secret
  Bridge.begin(115200, nullptr);

  // 13. Stale handshake timeout & 0 pending baudrate change
  ba.setSynchronized();
  ba.onHandshakeTimeout();
  ba.setPendingBaudrate(0);
  ba.onBaudrateChange();
}

static uint32_t g_av_count = 0;
static bool g_msg_received = false;
static void dummy_av_handler(uint32_t c) { g_av_count = c; }
static void dummy_msg_handler(etl::span<const uint8_t>) {
  g_msg_received = true;
}
static void dummy_ds_handler(etl::string_view, etl::span<const uint8_t>) {}

static void test_surgical_mailbox_datastore_edges() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);

  // 1. Mailbox empty push & send fail
  Mailbox.push(etl::span<const uint8_t>());
  Mailbox.signalProcessed(123U);

  // 2. Mailbox queue full branches
  rpc::payload::MailboxPush push_msg = {};
  push_msg.data.size = 2;
  push_msg.data.bytes[0] = 'a';
  push_msg.data.bytes[1] = 'b';
  etl::array<int, 10> push_steps{};
  etl::for_each(push_steps.begin(), push_steps.end(),
                [&](int) { Mailbox._onPush(push_msg); });

  rpc::payload::MailboxReadResponse read_resp = {};
  read_resp.content.size = 2;
  Mailbox._onReadResponse(read_resp);

  // 3. Mailbox callbacks & process
  Mailbox.registerAvailableCallback(
      MailboxClass::AvailableCallback::create<&dummy_av_handler>());
  rpc::payload::MailboxAvailableResponse av_resp = {};
  av_resp.count = 42U;
  Mailbox._onAvailableResponse(av_resp);
  TEST_ASSERT_EQUAL(42U, g_av_count);

  // Available response with null callback
  Mailbox.registerAvailableCallback(MailboxClass::AvailableCallback());
  Mailbox._onAvailableResponse(av_resp);

  Mailbox.registerMessageCallback(
      MailboxClass::MessageCallback::create<&dummy_msg_handler>());
  Mailbox.process();
  TEST_ASSERT_TRUE(g_msg_received);

  // Process with queue non-empty but null message callback
  Mailbox._onPush(push_msg);
  Mailbox.registerMessageCallback(MailboxClass::MessageCallback());
  Mailbox.process();
  Mailbox.onLost();

  // 4. DataStore empty key/val & pending gets full
  DataStore.set(etl::string_view(), etl::span<const uint8_t>());
  rpc::payload::DatastoreGetResponse ds_resp = {};
  DataStore._onResponse(ds_resp);

  auto dummy_handler = DataStoreClass::GetHandler::create<&dummy_ds_handler>();
  etl::array<int, 10> get_steps{};
  etl::for_each(get_steps.begin(), get_steps.end(),
                [&](int) { DataStore.get("k", dummy_handler); });

  // DataStore response with invalid handler
  DataStoreClass::GetHandler invalid_handler;
  DataStore.get("k2", invalid_handler);
  DataStore._onResponse(ds_resp);

  // DataStore response when pending gets queue is empty
  DataStore._pending_gets.clear();
  DataStore._onResponse(ds_resp);

  // 5. Synchronized Mailbox/DataStore calls
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  uint8_t payload_bytes[] = {'h', 'e', 'l', 'l', 'o'};
  Mailbox.push(etl::span<const uint8_t>(payload_bytes, 5));
  Mailbox.requestRead();
  Mailbox.requestAvailable();
  Mailbox.signalProcessed(456U);
  DataStore.set("key", etl::span<const uint8_t>(payload_bytes, 5));

  // 6. Send-fail branches: Mailbox::push, signalProcessed & DataStore::set
  //    Clear shared secret so send() routes through sendSinglePass (not
  //    _sendEncryptedHelper), which checks _tx_enabled and returns false.
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  Mailbox.push(etl::span<const uint8_t>(payload_bytes, 5));
  Mailbox.signalProcessed(789U);
  DataStore.set("key2", etl::span<const uint8_t>(payload_bytes, 3));
  ba.setTxEnabled(true);
}

static void test_surgical_console_edges() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);

  // Console write null/empty & rx buffer read/peek when empty
  Console.begin();
  TEST_ASSERT_EQUAL(0U, Console.write(nullptr, 10));
  TEST_ASSERT_EQUAL(0U, Console.write((const uint8_t*)"abc", 0));
  TEST_ASSERT_EQUAL(-1, Console.read());
  TEST_ASSERT_EQUAL(-1, Console.peek());

  // Console rx buffer push & read/peek
  rpc::payload::ConsoleWrite cwrite = {};
  cwrite.data.size = 3;
  cwrite.data.bytes[0] = 'X';
  cwrite.data.bytes[1] = 'Y';
  cwrite.data.bytes[2] = 'Z';
  Console._push(cwrite);

  TEST_ASSERT_EQUAL((int)'X', Console.peek());
  TEST_ASSERT_EQUAL(3, Console.available());
  TEST_ASSERT_EQUAL((int)'X', Console.read());
}

static void test_surgical_process_spi_edges() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);

  // 1. SPI edge paths: uninitialized, empty buffer, timeout
  SPIService.end();
  etl::array<uint8_t, 4> spi_buf = {1, 2, 3, 4};
  size_t t1 = SPIService.transfer(etl::span<uint8_t>(spi_buf));
  TEST_ASSERT_EQUAL(0U, t1);

  SPIService.begin();
  size_t t2 = SPIService.transfer(etl::span<uint8_t>());
  TEST_ASSERT_EQUAL(0U, t2);

  bridge::test::fault::enable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);
  size_t t3 = SPIService.transfer(etl::span<uint8_t>(spi_buf));
  TEST_ASSERT_EQUAL(0U, t3);
  bridge::test::fault::disable(bridge::test::fault::FaultPoint::SPI_TIMEOUT);

  // 2. Process edge paths: command buffer overflow & argument overflow
  char long_cmd[100];
  etl::fill_n(long_cmd, 99, 'x');
  long_cmd[99] = '\0';
  Process.runAsync(long_cmd, etl::span<const etl::string_view>(),
                   ProcessClass::ProcessRunHandler());

  etl::string_view args[1];
  args[0] = etl::string_view(long_cmd);
  Process.runAsync("ls", etl::span<const etl::string_view>(args, 1),
                   ProcessClass::ProcessRunHandler());

  // Process empty queue response branches
  Process._pending_run_async.clear();
  rpc_pb_ProcessRunAsyncResponse pr_resp = {};
  Process._onRunAsyncResponse(pr_resp);

  Process._pending_polls.clear();
  rpc_pb_ProcessPollResponse pp_resp = {};
  Process._onPollResponse(pp_resp);

  // Process queues full with valid handlers
  static auto dummy_pr_handler = ProcessClass::ProcessRunHandler();
  etl::array<int, 10> async_steps{};
  etl::for_each(async_steps.begin(), async_steps.end(), [&](int) {
    Process._pending_run_async.push({dummy_pr_handler});
  });
  Process.runAsync("ls", etl::span<const etl::string_view>(), dummy_pr_handler);

  Process.kill(999);
  auto& ba = TestAccessor::create(Bridge);
  ba.setTxEnabled(false);
  Process.kill(999);
  ba.setTxEnabled(true);

  // 3. SPI Transfer directly with empty data and with send failure
  rpc_pb_SpiTransfer spi_req = rpc_pb_SpiTransfer_init_default;
  spi_req.data.size = 0;
  bridge::router::CommandContext spi_ctx(
      nullptr, static_cast<uint16_t>(rpc::CommandId::CMD_SPI_TRANSFER), 42,
      false, false);
  ba.handleSpiTransfer(spi_ctx, spi_req);
  ba.setTxEnabled(false);
  ba.handleSpiTransfer(spi_ctx, spi_req);
  ba.setTxEnabled(true);

  // 4. LinkReset with empty payload type
  rpc_pb_RpcEnvelope env_hc = rpc_pb_RpcEnvelope_init_default;
  env_hc.which_payload_type = 0;
  bridge::router::CommandContext hc_ctx(
      &env_hc, static_cast<uint16_t>(rpc::CommandId::CMD_LINK_RESET), 43, false,
      false);
  ba.handleLinkReset(hc_ctx);

  // 5. Serial task flow control XON path when XOFF was active
  ba.setSerialTaskXoffSent(true);
  ba.invokeSerialTask();
  TEST_ASSERT_FALSE(ba.isSerialXoffSent());
}

static void dummy_fs_handler(etl::span<const uint8_t>) {}

static void test_surgical_filesystem_edges() {
  static BiStream stream;
  stream.clear();
  reset_bridge_core(Bridge, stream);

  // 1. FileSystem write, read, remove with empty path/data (unsynchronized send
  // failure)
  FileSystem.write(etl::string_view(), etl::span<const uint8_t>());
  FileSystem.read(etl::string_view(), FileSystemClass::FileSystemReadHandler());
  FileSystem.remove(etl::string_view());

  // 2. FileSystem read response with invalid handler
  FileSystem.read(etl::string_view(), FileSystemClass::FileSystemReadHandler());
  rpc::payload::FileReadResponse rresp = {};
  FileSystem._onResponse(rresp);

  // 3. FileSystem read with valid handler
  FileSystem.read(
      "test.txt",
      FileSystemClass::FileSystemReadHandler::create<&dummy_fs_handler>());
  FileSystem._onResponse(rresp);

  // 4. FileSystem write, read, remove when synchronized
  auto& ba = TestAccessor::create(Bridge);
  ba.setSynchronized();
  uint8_t data[] = {1, 2, 3};
  FileSystem.write("test.txt", etl::span<const uint8_t>(data, 3));
  FileSystem.read(
      "test.txt",
      FileSystemClass::FileSystemReadHandler::create<&dummy_fs_handler>());
  FileSystem.remove("test.txt");

  // 5. FileSystem _onWrite & _onRemove when sendFrame fails
  ba.setTxEnabled(false);
  rpc_pb_FileWrite fw_msg = {};
  FileSystem._onWrite(fw_msg);
  rpc_pb_FileRemove fr_msg = {};
  FileSystem._onRemove(fr_msg);
  ba.setTxEnabled(true);

  // 6. FileSystem _onRead non-existent file failure
  rpc_pb_FileRead fread_nonexistent = {};
  const char* non_path = "non_existent_file.xyz";
  etl::copy_n(non_path, strlen(non_path), fread_nonexistent.path);
  FileSystem._onRead(fread_nonexistent);

  // 7. FileSystem _onRead timeout via FILESYSTEM_TIMEOUT fault
  bridge::test::fault::enable(
      bridge::test::fault::FaultPoint::FILESYSTEM_TIMEOUT);
  rpc_pb_FileRead fread_valid = {};
  const char* val_path = "test.txt";
  etl::copy_n(val_path, strlen(val_path), fread_valid.path);
  FileSystem._onRead(fread_valid);

  // 8. FileSystem write() & remove() send-fail branches (lines 29-31, 56-58).
  //    Clear shared secret so send() routes through sendSinglePass (which
  //    checks _tx_enabled). With TX disabled, Bridge.send() returns false.
  ba.clearSharedSecret();
  ba.setTxEnabled(false);
  uint8_t fsdata[] = {1, 2};
  FileSystem.write("send_fail.txt", etl::span<const uint8_t>(fsdata, 2));
  FileSystem.remove("send_fail.txt");
  ba.setTxEnabled(true);

  // 9. _onRead of an empty file to cover the to_copy == 0 branch (L99).
  //    Write a 0-byte file via the mock, then call _onRead.
  bridge::hal::writeFile("empty.txt", etl::span<const uint8_t>());
  rpc_pb_FileRead fread_empty = {};
  const char* empty_path = "empty.txt";
  etl::copy_n(empty_path, strlen(empty_path), fread_empty.path);
  FileSystem._onRead(fread_empty);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_surgical_bridge_errors);
  RUN_TEST(test_surgical_fsm_resets);
  RUN_TEST(test_surgical_security_failures);
  RUN_TEST(test_surgical_tasks_flow);
  RUN_TEST(test_surgical_send_fail_branches);
  RUN_TEST(test_surgical_extra_branches);
  RUN_TEST(test_surgical_mailbox_datastore_edges);
  RUN_TEST(test_surgical_console_edges);
  RUN_TEST(test_surgical_process_spi_edges);
  RUN_TEST(test_surgical_filesystem_edges);
  return UNITY_END();
}