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
    _bridge._fsm.stabilized();
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

  void setStartupStabilizing(bool v) { 
    if (v) _bridge._fsm.resetFsm(); // resetFsm starts at STATE_STABILIZING
    else _bridge._fsm.stabilized();
  }
  bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  void assignSharedSecret(const uint8_t* first, const uint8_t* last) { _bridge._shared_secret.assign(first, last); }
  void setAckRetryLimit(uint8_t limit) { _bridge._ack_retry_limit = limit; }
  void onAckTimeout() { _bridge._onAckTimeout(); }
  void setAckTimeoutMs(uint16_t ms) { _bridge._ack_timeout_ms = ms; }
  uint8_t getAckRetryLimit() const { return _bridge._ack_retry_limit; }
  void markRxProcessed(const rpc::Frame& f) { _bridge._markRxProcessed(f); }
  bool isRecentDuplicateRx(const rpc::Frame& f) const { return _bridge._rx_history.contains(f.header.sequence_id); }
  void clearSharedSecret() { _bridge._shared_secret.clear(); }
  void fsmHandshakeComplete() { _bridge._fsm.handshakeComplete(); }
  void fsmSendCritical() { _bridge._fsm.sendCritical(); }
  void fsmCryptoFault() { _bridge._fsm.cryptoFault(); }
  void fsmResetFsm() { _bridge._fsm.resetFsm(); }
  void fsmHandshakeFailed() { _bridge._fsm.handshakeFailed(); }

  uint16_t getAckTimeoutMs() const { return _bridge._ack_timeout_ms; }

  void pushPendingTxFrame(uint16_t cmd, uint16_t len, const uint8_t* data) {
    BridgeClass::TxPayloadBuffer* buf = _bridge._tx_payload_pool.allocate();
    if (!buf) return;
    BridgeClass::PendingTxFrame f;
    f.command_id = cmd;
    f.payload_length = len;
    f.buffer = buf;
    if (len > 0) etl::copy_n(data, len, buf->data.data());
    _bridge._pending_tx_queue.push(f);
  }
  void flushPendingTxQueue() { _bridge._flushPendingTxQueue(); }

  size_t getRxHistorySize() const { return _bridge._rx_history.buffer.size(); }
  void onRxDedupe() { _bridge._onRxDedupe(); }

  void setPendingBaudrate(uint32_t br) { _bridge._pending_baudrate = br; }
  uint32_t getPendingBaudrate() const { return _bridge._pending_baudrate; }
  void onBaudrateChange() { _bridge._onBaudrateChange(); }

  void handleAck(uint16_t cmd) { _bridge._handleAck(cmd); }

  void applyTimingConfig(const uint8_t* data, size_t len) {
    rpc::payload::HandshakeConfig msg = {};
    msgpack::Decoder dec(data, len);
    if (msg.decode(dec)) {
      _bridge._applyTimingConfig(msg);
    }
  }

  void setLastParseError(rpc::FrameError err) { _bridge._last_parse_error = err; }
  void clearRxHistory() { _bridge._rx_history.clear(); }
  void handleMalformed(uint16_t cmd) { _bridge._handleMalformed(cmd); }
  size_t sharedSecretSize() const { return _bridge._shared_secret.size(); }

  void pushPendingTxFrame(uint16_t cmd, uint16_t len) {
    BridgeClass::TxPayloadBuffer* buf = _bridge._tx_payload_pool.allocate();
    if (!buf) return;
    BridgeClass::PendingTxFrame f;
    f.command_id = cmd;
    f.payload_length = len;
    f.buffer = buf;
    _bridge._pending_tx_queue.push(f);
  }

  void dispatch(const rpc::Frame& frame, uint16_t seq = 0) { _bridge._dispatchCommand(frame, seq); }
  void retransmitLastFrame() { _bridge._retransmitLastFrame(); }
  void computeHandshakeTag(const uint8_t* n, size_t nl, uint8_t* out) {
    _bridge._computeHandshakeTag(etl::span<const uint8_t>(n, nl), etl::span<uint8_t>(out, 16));
  }

  bool isSecurityCheckPassed(uint16_t cmd) const { return _bridge._isSecurityCheckPassed(cmd); }
  void onStartupStabilized() { _bridge._onStartupStabilized(); }
  void setSynchronized() { _bridge._fsm.stabilized(); _bridge._fsm.handshakeStart(); _bridge._fsm.handshakeComplete(); }
  bool getStartupStabilizing() const { return _bridge._fsm.isStabilizing(); }

  void handleSystemCommand(const rpc::Frame& frame) {
    bridge::router::CommandContext ctx{&frame, frame.header.command_id, false, false, frame.header.sequence_id};
    _bridge.onSystemCommand(ctx);
  }
  
  void routeStatusCommand(const bridge::router::CommandContext& ctx) { _bridge.onStatusCommand(ctx); }
  void routeSystemCommand(const bridge::router::CommandContext& ctx) { _bridge.onSystemCommand(ctx); }
  void routeGpioCommand(const bridge::router::CommandContext& ctx) { _bridge.onGpioCommand(ctx); }
  void routeConsoleCommand(const bridge::router::CommandContext& ctx) { _bridge.onConsoleCommand(ctx); }
  void routeDataStoreCommand(const bridge::router::CommandContext& ctx) { _bridge.onDataStoreCommand(ctx); }
  void routeMailboxCommand(const bridge::router::CommandContext& ctx) { _bridge.onMailboxCommand(ctx); }
  void routeFileSystemCommand(const bridge::router::CommandContext& ctx) { _bridge.onFileSystemCommand(ctx); }
  void routeProcessCommand(const bridge::router::CommandContext& ctx) { _bridge.onProcessCommand(ctx); }
  void routeSpiCommand(const bridge::router::CommandContext& ctx) { _bridge.onSpiCommand(ctx); }
  void routeUnknownCommand(const bridge::router::CommandContext& ctx) { _bridge.onUnknownCommand(ctx); }

  void handleReceivedFrame(etl::span<const uint8_t> data) { _bridge._handleReceivedFrame(data); }
  void handleEnterBootloader(const bridge::router::CommandContext& ctx) { _bridge._handleEnterBootloader(ctx); }
  void emitStatusStringView(rpc::StatusCode code, const char* msg) { _bridge.emitStatus(code, etl::string_view(msg)); }
  void emitStatusFlash(rpc::StatusCode code, const __FlashStringHelper* msg) { _bridge.emitStatus(code, msg); }
  void fsmTimeout() { _bridge._fsm.timeout(); }

  void handleGetVersion(const bridge::router::CommandContext& ctx) { _bridge._handleGetVersion(ctx); }
  void handleGetFreeMemory(const bridge::router::CommandContext& ctx) { _bridge._handleGetFreeMemory(ctx); }
  void handleDigitalWrite(const bridge::router::CommandContext& ctx) { _bridge._handleDigitalWrite(ctx); }
  void handleDigitalRead(const bridge::router::CommandContext& ctx) { _bridge._handleDigitalRead(ctx); }
  void handleAnalogRead(const bridge::router::CommandContext& ctx) { _bridge._handleAnalogRead(ctx); }

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
  void setXoffSent(bool v) { _c._flags.set(ConsoleClass::XOFF_SENT, v); }
  bool getXoffSent() const { return _c._flags.test(ConsoleClass::XOFF_SENT); }
  void setBegun(bool v) { _c._flags.set(ConsoleClass::BEGUN, v); }
  static ConsoleTestAccessor create(ConsoleClass& c) { return ConsoleTestAccessor(c); }
 private:
  ConsoleClass& _c;
};

class DataStoreTestAccessor {
 public:
  explicit DataStoreTestAccessor(DataStoreClass& ds) : _ds(ds) {}
  void clearPendingKeys() { _ds._pending_gets.clear(); }
  size_t pendingGetQueueSize() const { return _ds._pending_gets.size(); }
  bool trackPendingKey(etl::string_view key) {
    if (_ds._pending_gets.full()) return false;
    if (key.size() >= sizeof(rpc::payload::DatastoreGet::key)) return false;
    DataStoreClass::PendingGet pg;
    pg.key = key;
    _ds._pending_gets.push(pg);
    return true;
  }
  etl::string_view popPendingKey() {
    if (_ds._pending_gets.empty()) return {};
    auto k = _ds._pending_gets.front().key;
    _ds._pending_gets.pop();
    return k;
  }
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
  void pushPendingPid(int16_t pid) {
    ProcessClass::PendingPoll pp;
    pp.pid = pid;
    _p._pending_polls.push(pp);
  }
  size_t pendingPollQueueSize() const { return _p._pending_polls.size(); }
  static ProcessTestAccessor create(ProcessClass& p) { return ProcessTestAccessor(p); }
 private:
  ProcessClass& _p;
};

} // namespace test
} // namespace bridge

#endif
