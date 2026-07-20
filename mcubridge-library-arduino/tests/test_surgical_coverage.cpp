#define BRIDGE_ENABLE_TEST_INTERFACE
#include <BridgeFaultInjection.h>
#include <unity.h>

#include "Bridge.h"
#include "BridgeTestInterface.h"
#include "security/security.h"
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

  // 5. validate_frame_nonce with counter <= last_seen
  uint64_t last_seen = 100;
  etl::array<uint8_t, 12> old_nonce = {0};
  old_nonce[0] = 'M';
  old_nonce[1] = 'C';
  old_nonce[2] = 'U';
  old_nonce[11] = 50;
  bool old_ok = rpc::security::validate_frame_nonce(old_nonce, &last_seen);
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
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_surgical_bridge_errors);
  RUN_TEST(test_surgical_fsm_resets);
  RUN_TEST(test_surgical_security_failures);
  RUN_TEST(test_surgical_tasks_flow);
  RUN_TEST(test_surgical_send_fail_branches);
  return UNITY_END();
}