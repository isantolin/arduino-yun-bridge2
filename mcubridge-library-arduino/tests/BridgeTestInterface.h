#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#ifndef BRIDGE_ENABLE_TEST_INTERFACE
#define BRIDGE_ENABLE_TEST_INTERFACE
#endif

#include <etl/span.h>

#include "Bridge.h"

namespace bridge::test {

class TestAccessor {
 public:
  static TestAccessor create(BridgeClass& bridge) {
    return TestAccessor(bridge);
  }

  explicit TestAccessor(BridgeClass& bridge)
      : _bridge(bridge), _fsm(bridge._fsm) {}

  void setSynchronized() {
    _fsm.receive(bridge::fsm::EvHandshakeStart());
    _fsm.receive(bridge::fsm::EvHandshakeComplete());
  }

  bool isSynchronized() const { return _bridge.isSynchronized(); }
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

  // Restored locally for test compatibility
  void onStartupStabilized() { _fsm.receive(bridge::fsm::EvStabilized()); }

  template <typename TEvent>
  void trigger(const TEvent& ev) {
    _fsm.receive(ev);
  }

  void dispatch(const rpc_pb_McuFrame& frame) { _bridge._dispatchCommand(frame); }

  bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  void setSharedSecret(etl::span<const uint8_t> secret) {
    _bridge._shared_secret.assign(secret.begin(), secret.end());
  }

  void computeHandshakeTag(const uint8_t* nonce_ptr, size_t len,
                           uint8_t* tag_out) {
    if (_bridge._shared_secret.empty()) return;
    // [MEM-SAVE] Replaced manual handshake logic with centralized utility.
    etl::array<uint8_t, 32> out_tag_full;
    (void)rpc::security::handshake_authenticate_raw(
        _bridge._shared_secret.data(), _bridge._shared_secret.size(),
        nonce_ptr, len,
        nullptr, 0, // received_tag not used here
        out_tag_full.data());
    etl::copy_n(out_tag_full.begin(), 16, tag_out);
  }

  void onAckTimeout() { _bridge._onAckTimeout(); }
  void handleAck(uint16_t cmd) { _bridge._handleAck(cmd); }
  void handleGetVersion(const rpc_pb_McuFrame& frame) {
    _bridge._handleGetVersion(frame);
  }
  bool sendFrame(rpc::CommandId c, uint16_t seq, etl::span<const uint8_t> p) {
    return _bridge.sendFrame(c, seq, p);
  }
  void handleDigitalWriteCommand(const rpc_pb_McuFrame& frame) {
    _bridge._handleDigitalWriteCommand(frame);
  }
  void invokePacketReceived(etl::span<const uint8_t> p) {
    _bridge._onPacketReceived(p);
  }

  void invokeConsolePush(const rpc_pb_ConsoleWrite& cmsg) {
    (void)_bridge.send(rpc::CommandId::CMD_CONSOLE_WRITE, 0, cmsg);
  }
  bool isAwaitingAck() const {
    return _fsm.get_state_id() ==
           static_cast<etl::fsm_state_id_t>(bridge::fsm::StateId::AWAITING_ACK);
  }
  uint16_t getLastCommandId() const { return _bridge._last_command_id; }
  void onRxDedupe() { _bridge._onRxDedupe(); }
  void setPendingBaudrate(uint32_t b) { _bridge._pending_baudrate = b; }
  void onBaudrateChange() { _bridge._onBaudrateChange(); }
  void invokeWatchdog() { _bridge._watchdog_task.task_process_work(); }
  void invokeSerialTask() { _bridge._serial_task.task_process_work(); }
  void invokeTimerTask() { _bridge._timer_task.task_process_work(); }
  void setSerialTaskXoffSent(bool value) { _bridge._serial_task.xoff_sent = value; }
  void setSerialTaskBridgeNull() { _bridge._serial_task.bridge = nullptr; }
  void setTimerTaskBridgeNull() { _bridge._timer_task.bridge = nullptr; }
  void setTimerLastTick(uint32_t tick) { _bridge._timer_task.last_tick_ms = tick; }
  void setHardwareSerial(HardwareSerial* serial) { _bridge._hardware_serial = serial; }
  void clearPendingTxQueue() { _bridge._clearPendingTxQueue(); }
  void exhaustTxPayloadPool() {
    exhaustTxPayloadPoolRecursive();
  }
  void enqueueNullPendingFrame(uint16_t command_id, uint16_t sequence_id, size_t length) {
    _bridge._pending_tx_queue.push_back({command_id, sequence_id, nullptr, length});
  }
  void clearSynchronized() { _fsm.receive(bridge::fsm::EvReset()); }
  void onBootloaderDelay() { _bridge._onBootloaderDelay(); }
  void applyTimingConfig(const rpc_pb_HandshakeConfig& msg) {
    _bridge._applyTimingConfig(msg);
  }
  void clearSharedSecret() { _bridge._shared_secret.clear(); }
  bool isSecurityCheckPassed(uint16_t cmd) const {
    return _bridge._isSecurityCheckPassed(cmd);
  }

  void setIdle() {
    if (!_fsm.is_started()) _fsm.start();
    _fsm.receive(bridge::fsm::EvReset());
  }

  size_t getObserverCount() const { return _bridge._observers.size(); }

 private:
  BridgeClass& _bridge;
  bridge::fsm::BridgeFsm& _fsm;

  void exhaustTxPayloadPoolRecursive() {
    auto* slot = _bridge._tx_payload_pool.allocate();
    if (slot == nullptr) return;
    exhaustTxPayloadPoolRecursive();
  }
};

}  // namespace bridge::test

#endif
