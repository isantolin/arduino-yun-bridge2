#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE
#endif

#include "Bridge.h"
#include <etl/span.h>

namespace bridge::test {

class TestAccessor {
 public:
  static TestAccessor create(BridgeClass& bridge) {
    return TestAccessor(bridge);
  }

  explicit TestAccessor(BridgeClass& bridge) : _bridge(bridge), _fsm(bridge._fsm) {}

  void setSynchronized() {
    _fsm.receive(bridge::fsm::EvHandshakeStart());
    _fsm.receive(bridge::fsm::EvHandshakeComplete());
  }

  bool isSynchronized() const { return _bridge.isSynchronized(); }
  bool isFault() const {
    return _fsm.get_state_id() == static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::FAULT);
  }
  etl::fsm_state_id_t get_state_id() const { return _fsm.get_state_id(); }
  bool isUnsynchronized() const {
    return _fsm.get_state_id() == static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::UNSYNCHRONIZED);
  }
  bool getStartupStabilizing() const {
    return _fsm.get_state_id() == static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::STARTUP);
  }

  // Restored locally for test compatibility
  void onStartupStabilized() {
    _fsm.receive(bridge::fsm::EvStabilized());
  }
  
  template<typename TEvent>
  void trigger(const TEvent& ev) {
    _fsm.receive(ev);
  }

  void dispatch(const rpc::Frame& frame) { _bridge._dispatchCommand(frame); }

  bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  void setSharedSecret(etl::span<const uint8_t> secret) {
    _bridge._shared_secret.assign(secret.begin(), secret.end());
  }

  void computeHandshakeTag(const uint8_t* nonce_ptr, size_t len, uint8_t* tag_out) {
    if (_bridge._shared_secret.empty()) return;
    etl::span<const uint8_t> nonce(nonce_ptr, len);
    etl::array<uint8_t, 32> handshake_key;
    rpc::security::hkdf_sha256(
        etl::span<uint8_t>(handshake_key),
        etl::span<const uint8_t>(_bridge._shared_secret),
        etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_SALT),
        etl::span<const uint8_t>(rpc::RPC_HANDSHAKE_HKDF_INFO_AUTH));

    etl::array<uint8_t, 32> full_tag;
    Hmac hmac_engine;
    wc_HmacSetKey(&hmac_engine, WC_SHA256, handshake_key.data(), 32);
    wc_HmacUpdate(&hmac_engine, nonce.data(), static_cast<word32>(nonce.size()));
    wc_HmacFinal(&hmac_engine, full_tag.data());

    etl::copy_n(full_tag.begin(), 16, tag_out);
  }

  void onAckTimeout() { _bridge._onAckTimeout(); }
  void handleAck(uint16_t cmd) { _bridge._handleAck(cmd); }
  void handleGetVersion(const bridge::router::CommandContext& ctx) {
    _bridge._handleGetVersion(ctx);
  }
  bool sendFrame(rpc::CommandId c, uint16_t seq, etl::span<const uint8_t> p) {
    return _bridge.sendFrame(c, seq, p);
  }
  void handleDigitalWriteCommand(const bridge::router::CommandContext& ctx) {
    _bridge._handleDigitalWriteCommand(ctx);
  }
  void invokePacketReceived(etl::span<const uint8_t> p) {
    _bridge._onPacketReceived(p);
  }

  void invokeConsolePush(const rpc::payload::ConsoleWrite& cmsg) {
    (void)_bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, cmsg);
  }
  bool isAwaitingAck() const { 
    return _fsm.get_state_id() == static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::AWAITING_ACK); 
  }
  uint16_t getLastCommandId() const { return _bridge._last_command_id; }
  void onRxDedupe() { _bridge._onRxDedupe(); }
  void setPendingBaudrate(uint32_t b) { _bridge._pending_baudrate = b; }
  void onBaudrateChange() { _bridge._onBaudrateChange(); }
  void invokeWatchdog() { _bridge._watchdog_task.task_process_work(); }
  void invokeSerialTask() { _bridge._serial_task.task_process_work(); }
  void clearSynchronized() {
    _fsm.receive(bridge::fsm::EvReset());
  }
  void onBootloaderDelay() { _bridge._onBootloaderDelay(); }

  void setIdle() {
    if (!_fsm.is_started()) _fsm.start();
    _fsm.receive(bridge::fsm::EvReset());
  }

  size_t getObserverCount() const { return _bridge._observers.size(); }

 private:
  BridgeClass& _bridge;
  bridge::fsm::BridgeFsm& _fsm;
};

}  // namespace bridge::test

#endif
