#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#include "Bridge.h"

namespace bridge {
namespace test {

class TestAccessor {
 public:
  static TestAccessor create(BridgeClass& bridge) { return TestAccessor(bridge); }
  explicit TestAccessor(BridgeClass& bridge) : _bridge(bridge) {}

  bool isAwaitingAck() const { return _bridge._fsm.isAwaitingAck(); }
  bool isFault() const { return _bridge._fsm.isFault(); }
  bool isUnsynchronized() const { return _bridge._fsm.get_state_id() == bridge::fsm::StateId::UNSYNCHRONIZED; }
  bool getStartupStabilizing() const { return _bridge._fsm.get_state_id() == bridge::fsm::StateId::STARTUP; }

  void onStartupStabilized() { _bridge._onStartupStabilized(); }
  void dispatch(const rpc::Frame& frame) { _bridge._dispatchCommand(frame); }

  bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  void setSharedSecret(etl::span<const uint8_t> secret) {
    _bridge._shared_secret.assign(secret.begin(), secret.end());
  }
  void computeHandshakeTag(const uint8_t* nonce, size_t len, uint8_t* tag) {
    _bridge._computeHandshakeTag(etl::span<const uint8_t>(nonce, len), etl::span<uint8_t>(tag, rpc::RPC_HANDSHAKE_TAG_LENGTH));
  }

  void handleGetVersion(const bridge::router::CommandContext& ctx) { _bridge._handleGetVersion(ctx); }
  void handleGetFreeMemory(const bridge::router::CommandContext& ctx) { _bridge._handleGetFreeMemory(ctx); }
  void handleLinkSync(const bridge::router::CommandContext& ctx) { _bridge._handleLinkSync(ctx); }
  void handleLinkReset(const bridge::router::CommandContext& ctx) { _bridge._handleLinkReset(ctx); }
  void handleGetCapabilities(const bridge::router::CommandContext& ctx) { _bridge._handleGetCapabilities(ctx); }

  bool isSynchronized() const { return _bridge.isSynchronized(); }
  void onAckTimeout() { _bridge._onAckTimeout(); }
  void forceTimeout() { _bridge._fsm.timeout(); }
  void setLastParseError(rpc::FrameError e) { _bridge._last_parse_error = e; }
  rpc::FrameError getLastParseError() const { return _bridge._last_parse_error; }
  uint8_t getAckRetryLimit() const { return _bridge._retry_limit; }
  void setRetryCount(uint8_t c) { _bridge._retry_count = c; }
  void clearRxHistory() { _bridge._rx_history.clear(); }
  bool isRecentDuplicateRx(const rpc::Frame& f) const { return _bridge._rx_history.exists(f.header.sequence_id); }
  void markRxProcessed(const rpc::Frame& f) { _bridge._rx_history.push(f.header.sequence_id); }

  void setIdle() {
    _bridge._fsm.resetFsm();
  }

  void setSynchronized() {
    _bridge._fsm.handshakeStart();
    _bridge._fsm.handshakeComplete();
  }

 private:
  BridgeClass& _bridge;
};

} // namespace test
} // namespace bridge

#endif
