#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#include "Bridge.h"

namespace bridge {
namespace test {

class TestAccessor {
 public:
  explicit TestAccessor(BridgeClass& bridge) : _bridge(bridge) {}
  bool isAwaitingAck() const { return _bridge.isAwaitingAck(); }
  bool isIdle() const { return _bridge.isIdle(); }
  bool isUnsynchronized() const { return _bridge.isUnsynchronized(); }
  bool isFault() const { return _bridge.isFault(); }
  
  void setUnsynchronized() { _bridge._fsm.resetFsm(); }
  void setIdle() {
    _bridge._fsm.resetFsm();
    _bridge._fsm.handshakeStart();
    _bridge._fsm.handshakeComplete();
  }
  void setAwaitingAck() { setIdle(); _bridge._fsm.sendCritical(); }
  void setFault() { _bridge._fsm.cryptoFault(); }
  void fsmHandshakeStart() { _bridge._fsm.handshakeStart(); }

  uint16_t getLastCommandId() const { return _bridge._last_command_id; }
  void setLastCommandId(uint16_t id) { _bridge._last_command_id = id; }
  uint8_t getRetryCount() const { return _bridge._retry_count; }
  void setRetryCount(uint8_t count) { _bridge._retry_count = count; }

  void setStartupStabilizing(bool v) { _bridge._startup_stabilizing = v; }
  bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  void assignSharedSecret(const uint8_t* first, const uint8_t* last) { _bridge._shared_secret.assign(first, last); }

  void dispatch(const rpc::Frame& frame) { _bridge.dispatch(frame); }
  void retransmitLastFrame() { _bridge._retransmitLastFrame(); }
  void computeHandshakeTag(const uint8_t* n, size_t nl, uint8_t* out) {
    _bridge._computeHandshakeTag(etl::span<const uint8_t>(n, nl), out);
  }
  
  void routeStatusCommand(const bridge::router::CommandContext& ctx) { _bridge.onStatusCommand(ctx); }
  void routeSystemCommand(const bridge::router::CommandContext& ctx) { _bridge.onSystemCommand(ctx); }
  void routeGpioCommand(const bridge::router::CommandContext& ctx) { _bridge.onGpioCommand(ctx); }
  void routeConsoleCommand(const bridge::router::CommandContext& ctx) { _bridge.onConsoleCommand(ctx); }
  void routeDataStoreCommand(const bridge::router::CommandContext& ctx) { _bridge.onDataStoreCommand(ctx); }
  void routeMailboxCommand(const bridge::router::CommandContext& ctx) { _bridge.onMailboxCommand(ctx); }
  void routeFileSystemCommand(const bridge::router::CommandContext& ctx) { _bridge.onFileSystemCommand(ctx); }
  void routeProcessCommand(const bridge::router::CommandContext& ctx) { _bridge.onProcessCommand(ctx); }
  void routeUnknownCommand(const bridge::router::CommandContext& ctx) { _bridge.onUnknownCommand(ctx); }

  static TestAccessor create(BridgeClass& bridge) { return TestAccessor(bridge); }
 private:
  BridgeClass& _bridge;
};

class ConsoleTestAccessor {
 public:
  explicit ConsoleTestAccessor(ConsoleClass& c) : _c(c) {}
  bool isRxBufferEmpty() const { return _c._rx_buffer.empty(); }
  void clearRxBuffer() { _c._rx_buffer.clear(); }
  void pushRxByte(uint8_t b) { _c._rx_buffer.push(b); }
  bool isTxBufferFull() const { return _c._tx_buffer.full(); }
  void clearTxBuffer() { _c._tx_buffer.clear(); }
  void pushTxByte(uint8_t b) { _c._tx_buffer.push_back(b); }
  static ConsoleTestAccessor create(ConsoleClass& c) { return ConsoleTestAccessor(c); }
 private:
  ConsoleClass& _c;
};

class DataStoreTestAccessor {
 public:
  explicit DataStoreTestAccessor(DataStoreClass& ds) : _ds(ds) {}
  void clearPendingKeys() { _ds._pending_gets.clear(); }
  static DataStoreTestAccessor create(DataStoreClass& ds) { return DataStoreTestAccessor(ds); }
 private:
  DataStoreClass& _ds;
};

class MailboxTestAccessor {
 public:
  explicit MailboxTestAccessor(MailboxClass& m) : _m(m) {}
  static MailboxTestAccessor create(MailboxClass& m) { return MailboxTestAccessor(m); }
 private:
  MailboxClass& _m;
};

class FileSystemTestAccessor {
 public:
  explicit FileSystemTestAccessor(FileSystemClass& fs) : _fs(fs) {}
  static FileSystemTestAccessor create(FileSystemClass& fs) { return FileSystemTestAccessor(fs); }
 private:
  FileSystemClass& _fs;
};

class ProcessTestAccessor {
 public:
  explicit ProcessTestAccessor(ProcessClass& p) : _p(p) {}
  void clearPendingPids() { _p._pending_async_runs.clear(); _p._pending_polls.clear(); }
  static ProcessTestAccessor create(ProcessClass& p) { return ProcessTestAccessor(p); }
 private:
  ProcessClass& _p;
};

} // namespace test
} // namespace bridge

#endif
