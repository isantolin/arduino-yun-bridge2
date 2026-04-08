#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#include "Bridge.h"
#include "services/DataStore.h"
#include "services/Mailbox.h"
#include "services/FileSystem.h"
#include "services/Process.h"

namespace bridge {
namespace test {

class TestAccessor {
 public:
  explicit TestAccessor(BridgeClass& b) : _bridge(b) {}
  static TestAccessor create(BridgeClass& b) { return TestAccessor(b); }

  void onStartupStabilized() { _bridge._onStartupStabilized(); }
  void onAckTimeout() { _bridge._onAckTimeout(); }
  bool isSynchronized() const { return _bridge.isSynchronized(); }
  bool isAwaitingAck() const { return _bridge._fsm.isAwaitingAck(); }
  bool isFault() const { return _bridge._fsm.isFault(); }
  bool isUnsynchronized() const { return _bridge._fsm.get_state_id() == bridge::fsm::StateId::UNSYNCHRONIZED; }
  bool getStartupStabilizing() const { return _bridge._fsm.get_state_id() == bridge::fsm::StateId::STARTUP; }
  
  void setIdle() { 
    _bridge._fsm.resetFsm();
    _bridge._fsm.stabilized();
    _bridge._fsm.handshakeStart();
    _bridge._fsm.handshakeComplete();
  }
  
  bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  void setSharedSecret(etl::span<const uint8_t> s) { _bridge._shared_secret.assign(s.begin(), s.end()); }
  
  // [TEST COMPAT]
  void assignSharedSecret(const uint8_t* begin, const uint8_t* end) {
    _bridge._shared_secret.assign(begin, end);
  }

  void dispatch(const rpc::Frame& frame) { _bridge._dispatchCommand(frame); }
  void retransmitLastFrame() { _bridge._retransmitLastFrame(); }
  void computeHandshakeTag(const uint8_t* n, size_t nl, uint8_t* out) { 
    _bridge._computeHandshakeTag(etl::span<const uint8_t>(n, nl), etl::span<uint8_t>(out, 16)); 
  }
  
  void setSynchronized() { 
    _bridge._fsm.stabilized(); 
    _bridge._fsm.handshakeStart(); 
    _bridge._fsm.handshakeComplete(); 
  }

  void handleGetVersion(const bridge::router::CommandContext& ctx) { _bridge._handleGetVersion(ctx); }
  void handleGetFreeMemory(const bridge::router::CommandContext& ctx) { _bridge._handleGetFreeMemory(ctx); }

  // [COVERAGE COMPAT]
  void clearRxHistory() { _bridge._rx_history.clear(); }
  bool isRecentDuplicateRx(const rpc::Frame& f) const { return _bridge._rx_history.exists(f.header.sequence_id); }
  void markRxProcessed(const rpc::Frame& f) { _bridge._markRxProcessed(f); }
  
  void setLastParseError(rpc::FrameError err) { _bridge._last_parse_error = err; }
  rpc::FrameError getLastParseError() const { return _bridge._last_parse_error; }

  uint8_t getRetryCount() const { return _bridge._retry_count; }
  void setRetryCount(uint8_t c) { _bridge._retry_count = c; }
  uint8_t getAckRetryLimit() const { return _bridge._retry_limit; }
  
  void forceTimeout() { _bridge._fsm.timeout(); }
  
 private:
  BridgeClass& _bridge;
};

class DataStoreTestAccessor {
 public:
  explicit DataStoreTestAccessor(DataStoreClass& ds) : _ds(ds) {}
  static DataStoreTestAccessor create(DataStoreClass& ds) { return DataStoreTestAccessor(ds); }
  void pushPendingKey(const char* key) {
    DataStoreClass::PendingGet pg;
    strncpy(pg.key, key, 15); pg.key[15] = '\0';
    _ds._pending_gets.push(pg);
  }
 private:
  DataStoreClass& _ds;
};

class ProcessTestAccessor {
 public:
  explicit ProcessTestAccessor(ProcessClass& p) : _p(p) {}
  static ProcessTestAccessor create(ProcessClass& p) { return ProcessTestAccessor(p); }
  void clearPendingPids() { while(!_p._pending_polls.empty()) _p._pending_polls.pop(); }
  void pushPendingPid(int32_t pid) {
    ProcessClass::PendingPoll pp = {pid};
    _p._pending_polls.push(pp);
  }
  size_t pendingPollQueueSize() const { return _p._pending_polls.size(); }
 private:
  ProcessClass& _p;
};

} // namespace test
} // namespace bridge

#endif
