#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE
#endif

#include <etl/span.h>
#include "Bridge.h"

namespace bridge::test {

class TestAccessor : public BridgeClass {
 public:
  explicit TestAccessor(Stream& s) : BridgeClass(s) {}

  static TestAccessor& create(BridgeClass& b) {
    return static_cast<TestAccessor&>(b);
  }

  void setSynchronized() {
    _fsm.receive(bridge::fsm::EvHandshakeStart());
    _fsm.receive(bridge::fsm::EvHandshakeComplete());
  }

  bool isFault() const {
    return _fsm.get_state_id() ==
           static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::FAULT);
  }
  etl::fsm_state_id_t get_state_id() const { return _fsm.get_state_id(); }
  bool isUnsynchronized() const {
    return _fsm.get_state_id() == static_cast<etl::fsm_state_id_t>(
                                      bridge::fsm::StateId::UNSYNCHRONIZED);
  }
  bool getStartupStabilizing() const {
    return _fsm.get_state_id() ==
           static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::STARTUP);
  }

  void onStartupStabilized() { _fsm.receive(bridge::fsm::EvStabilized()); }

  template <typename TEvent>
  void trigger(const TEvent& ev) {
    _fsm.receive(ev);
  }

  void dispatch(const rpc_pb_RpcEnvelope& frame) { _dispatchCommand(frame); }

  bool isSharedSecretEmpty() const { return _shared_secret.empty(); }
  void setSharedSecret(etl::span<const uint8_t> secret) {
    _shared_secret.assign(secret.begin(), secret.end());
  }

  void computeHandshakeTag(const uint8_t* nonce_ptr, size_t len,
                           uint8_t* tag_out) {
    if (_shared_secret.empty()) return;
    etl::array<uint8_t, 32> out_tag_full;
    if (rpc::security::handshake_authenticate(
            etl::span<const uint8_t>(_shared_secret),
            etl::span<const uint8_t>(nonce_ptr, len),
            etl::span<const uint8_t>(),
            etl::span<uint8_t>(out_tag_full))) {
      etl::copy_n(out_tag_full.begin(), 16, tag_out);
    }
  }

  void onAckTimeout() { _onAckTimeout(); }
  void handleAck(uint16_t cmd) { _handleAck(cmd); }
  void handleGetVersion(const bridge::router::CommandContext& ctx) {
    _handleGetVersion(ctx);
  }
  void handleDigitalWrite(const rpc_pb_DigitalWrite& m) { _handleDigitalWrite(m); }
  void invokePacketReceived(etl::span<const uint8_t> p) {
    _onPacketReceived(p);
  }

  void invokeConsolePush(const rpc::payload::ConsoleWrite& cmsg) {
    (void)send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, cmsg);
  }
  bool isAwaitingAck() const {
    return _fsm.get_state_id() ==
           static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::AWAITING_ACK);
  }
  uint16_t getLastCommandId() const { return _last_command_id; }
  void onRxDedupe() { _onRxDedupe(); }
  void setPendingBaudrate(uint32_t b) { _pending_baudrate = b; }
  void onBaudrateChange() { _onBaudrateChange(); }
  void invokeWatchdog() { _watchdog_task.task_process_work(); }
  void invokeSerialTask() { _serial_task.task_process_work(); }
  void invokeTimerTask() { _timer_task.task_process_work(); }
  void setSerialTaskXoffSent(bool value) { _serial_task.xoff_sent = value; }
  void setSerialTaskBridgeNull() { _serial_task.bridge = nullptr; }
  void setTimerTaskBridgeNull() { _timer_task.bridge = nullptr; }
  void setTimerLastTick(uint32_t tick) { _timer_task.last_tick_ms = tick; }
  void setHardwareSerial(HardwareSerial* serial) { _hardware_serial = serial; }
  void clearPendingTxQueue() { _clearPendingTxQueue(); }
  void exhaustTxPayloadPool() { exhaustTxPayloadPoolRecursive(); }
  void enqueueNullPendingFrame(uint16_t command_id, uint16_t sequence_id,
                               size_t length) {
    _pending_tx_queue.push_back({command_id, sequence_id, nullptr, length});
  }
  void clearSynchronized() { _fsm.receive(bridge::fsm::EvReset()); }
  void onBootloaderDelay() { _onBootloaderDelay(); }
  void applyTimingConfig(const rpc::payload::HandshakeConfig& msg) {
    _applyTimingConfig(msg);
  }
  void clearSharedSecret() { _shared_secret.clear(); }
  bool isSecurityCheckPassed(uint16_t cmd) const {
    return _isSecurityCheckPassed(cmd);
  }

  void setSessionKey(etl::span<const uint8_t> key) {
    etl::copy_n(key.begin(), etl::min(key.size(), _session_key.size()),
                _session_key.begin());
  }
  void setRxNonceCounter(uint64_t counter) { _rx_nonce_counter = counter; }

  void setIdle() {
    if (!_fsm.is_started()) _fsm.start();
    _fsm.receive(bridge::fsm::EvReset());
  }

 private:
  void exhaustTxPayloadPoolRecursive() {
    auto* slot = _tx_payload_pool.allocate();
    if (slot == nullptr) return;
    exhaustTxPayloadPoolRecursive();
  }
};

}  // namespace bridge::test

#endif
