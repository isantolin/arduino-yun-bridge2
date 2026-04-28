#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#include "Bridge.h"

namespace bridge {
namespace test {

/**
 * [SIL-2] TestAccessor: Uses inheritance to expose protected BridgeClass
 * members. This eliminates the 'friend' declaration in production code.
 */
class TestAccessor : public BridgeClass {
 public:
  static TestAccessor& create(BridgeClass& bridge) {
    return static_cast<TestAccessor&>(bridge);
  }

  bool isAwaitingAck() const { return _fsm.isAwaitingAck(); }
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

  void onStartupStabilized() { _onStartupStabilized(); }
  void dispatch(const rpc::Frame& frame) { _dispatchCommand(frame); }

  bool isSharedSecretEmpty() const { return _shared_secret.empty(); }
  void setSharedSecret(etl::span<const uint8_t> secret) {
    _shared_secret.assign(secret.begin(), secret.end());
  }
  void computeHandshakeTag(const uint8_t* nonce, size_t len, uint8_t* tag) {
    _computeHandshakeTag(
        etl::span<const uint8_t>(nonce, len),
        etl::span<uint8_t>(tag, rpc::RPC_HANDSHAKE_TAG_LENGTH));
  }

  void handleGetVersion(const bridge::router::CommandContext& ctx) {
    _handleGetVersion(ctx);
  }
  void handleGetFreeMemory(const bridge::router::CommandContext& ctx) {
    _handleGetFreeMemory(ctx);
  }
  void handleLinkSync(const bridge::router::CommandContext& ctx) {
    _handleLinkSync(ctx);
  }
  void handleLinkReset(const bridge::router::CommandContext& ctx) {
    _handleLinkReset(ctx);
  }
  void handleGetCapabilities(const bridge::router::CommandContext& ctx) {
    _handleGetCapabilities(ctx);
  }

  bool isSynchronized() const { return BridgeClass::isSynchronized(); }
  void onAckTimeout() { _onAckTimeout(); }
  void forceTimeout() { _fsm.receive(bridge::fsm::EvTimeout()); }
  void trigger(const etl::imessage& msg) { _fsm.receive(msg); }
  void setLastParseError(rpc::FrameError e) { _last_parse_error = e; }
  rpc::FrameError getLastParseError() const { return _last_parse_error; }
  uint8_t getAckRetryLimit() const { return _retry_limit; }
  void setRetryCount(uint8_t c) { _retry_count = c; }
  void clearRxHistory() { _rx_history.clear(); }
  bool isRecentDuplicateRx(const rpc::Frame& f) const {
    return etl::find(_rx_history.begin(), _rx_history.end(),
                     f.header.sequence_id) != _rx_history.end();
  }
  bool isTxEnabled() const { return _tx_enabled; }
  void setTxEnabled(bool enabled) { _tx_enabled = enabled; }
  void startFsm() {
    if (!_fsm.is_started()) _fsm.start();
  }
  void setPendingBaudrate(uint32_t b) { _pending_baudrate = b; }

  void setIdle() {
    if (!_fsm.is_started()) _fsm.start();
    _fsm.receive(bridge::fsm::EvReset());
  }

  void setSynchronized() {
    _fsm.receive(bridge::fsm::EvStabilized());
    _fsm.receive(bridge::fsm::EvHandshakeStart());
    _fsm.receive(bridge::fsm::EvHandshakeComplete());
  }
};

}  // namespace test
}  // namespace bridge

#endif
