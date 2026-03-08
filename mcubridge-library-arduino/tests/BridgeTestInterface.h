/*
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin
 *
 * Test interface for BridgeClass and subsystem classes - provides
 * controlled access to internal state for unit testing without
 * using the UB-inducing #define private public hack.
 */
#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#ifdef BRIDGE_ENABLE_TEST_INTERFACE

#include <string.h>  // memcpy
#include "Bridge.h"

namespace bridge {
namespace test {

class TestAccessor {
 public:
  explicit TestAccessor(BridgeClass& bridge) : _bridge(bridge) {}

  bool isAwaitingAck() const { return _bridge.isAwaitingAck(); }
  bool isIdle() const { return _bridge.isIdle(); }
  bool isUnsynchronized() const { return _bridge._fsm.isUnsynchronized(); }
  bool isFault() const { return _bridge.isFault(); }

  void setUnsynchronized() { _bridge._fsm.resetFsm(); }
  void setIdle() {
    _bridge._fsm.resetFsm();
    _bridge._fsm.handshakeStart();
    _bridge._fsm.handshakeComplete();
  }
  void setAwaitingAck() {
    setIdle();
    _bridge._fsm.sendCritical();
  }
  void setFault() { _bridge._fsm.cryptoFault(); }
  void setSynchronized(bool synchronized) {
    if (synchronized) setIdle(); else setUnsynchronized();
  }

  void fsmResetFsm() { _bridge._fsm.resetFsm(); }
  void fsmHandshakeStart() { _bridge._fsm.handshakeStart(); }
  void fsmHandshakeComplete() { _bridge._fsm.handshakeComplete(); }
  void fsmHandshakeFailed() { _bridge._fsm.handshakeFailed(); }
  void fsmSendCritical() { _bridge._fsm.sendCritical(); }
  void fsmCryptoFault() { _bridge._fsm.cryptoFault(); }

  uint16_t getLastCommandId() const { return _bridge._last_command_id; }
  void setLastCommandId(uint16_t id) { _bridge._last_command_id = id; }
  uint8_t getRetryCount() const { return _bridge._retry_count; }
  void setRetryCount(uint8_t count) { _bridge._retry_count = count; }
  uint16_t getAckTimeoutMs() const { return _bridge._ack_timeout_ms; }
  void setAckTimeoutMs(uint16_t ms) { _bridge._ack_timeout_ms = ms; }
  uint8_t getAckRetryLimit() const { return _bridge._ack_retry_limit; }
  void setAckRetryLimit(uint8_t limit) { _bridge._ack_retry_limit = limit; }
  uint32_t getResponseTimeoutMs() const { return _bridge._response_timeout_ms; }
  void setResponseTimeoutMs(uint32_t ms) { _bridge._response_timeout_ms = ms; }

  size_t getRxHistorySize() const { return _bridge._rx_history.size(); }
  uint32_t getRxHistoryCrc(size_t i) const { return _bridge._rx_history[i].crc; }
  void clearRxHistory() { _bridge._rx_history.clear(); }

  bool getStartupStabilizing() const { return _bridge._startup_stabilizing; }
  void setStartupStabilizing(bool v) { _bridge._startup_stabilizing = v; }

  bool isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  size_t sharedSecretSize() const { return _bridge._shared_secret.size(); }
  void assignSharedSecret(const uint8_t* first, const uint8_t* last) { _bridge._shared_secret.assign(first, last); }
  void clearSharedSecret() { _bridge._shared_secret.clear(); }

  void setLastParseError(rpc::FrameError err) { _bridge._last_parse_error = err; }
  void clearLastParseError() { _bridge._last_parse_error.reset(); }
  void setHardwareSerial(HardwareSerial* s) { _bridge._hardware_serial = s; }
  uint32_t getPendingBaudrate() const { return _bridge._pending_baudrate; }
  void setPendingBaudrate(uint32_t baud) { _bridge._pending_baudrate = baud; }

  void dispatch(const rpc::Frame& frame) { _bridge.dispatch(frame); }
  void retransmitLastFrame() { _bridge._retransmitLastFrame(); }
  void onAckTimeout() { _bridge.onAckTimeout(); }
  void onBaudrateChange() { _bridge.onBaudrateChange(); }
  void onRxDedupe() { _bridge.onRxDedupe(); }
  void onStartupStabilized() { _bridge.onStartupStabilized(); }
  bool isRecentDuplicateRx(const rpc::Frame& f) const { return _bridge._isRecentDuplicateRx(f); }
  void markRxProcessed(const rpc::Frame& f) { _bridge._markRxProcessed(f); }
  void applyTimingConfig(const uint8_t* p, size_t len) { rpc::Frame f{}; f.header.payload_length = static_cast<uint16_t>(len); if (p && len > 0) memcpy(f.payload.data(), p, len); _bridge._applyTimingConfig(f); }
  bool requiresAck(uint16_t cmd) const { return rpc::requires_ack(cmd); }
  void handleAck(uint16_t cmd) { _bridge._handleAck(cmd); }
  void handleMalformed(uint16_t cmd) { _bridge._handleMalformed(cmd); }

  void handleSystemCommand(const rpc::Frame& f) { bridge::router::CommandMessage msg(&f, f.header.command_id, false, rpc::requires_ack(f.header.command_id)); _bridge.onSystemCommand(msg); }
  void handleGpioCommand(const rpc::Frame& f) { bridge::router::CommandMessage msg(&f, f.header.command_id, false, rpc::requires_ack(f.header.command_id)); _bridge.onGpioCommand(msg); }
  void computeHandshakeTag(const uint8_t* n, size_t nl, uint8_t* out) { _bridge._computeHandshakeTag(etl::span<const uint8_t>(n, nl), out); }

  void routeStatusCommand(const bridge::router::CommandMessage& msg) { _bridge.onStatusCommand(msg); }
  void routeSystemCommand(const bridge::router::CommandMessage& msg) { _bridge.onSystemCommand(msg); }
  void routeGpioCommand(const bridge::router::CommandMessage& msg) { _bridge.onGpioCommand(msg); }
  void routeUnknownCommand(const bridge::router::CommandMessage& msg) { _bridge.onUnknownCommand(msg); }

  static TestAccessor create(BridgeClass& bridge) { return TestAccessor(bridge); }

 private:
  BridgeClass& _bridge;
};

class ConsoleTestAccessor {
 public:
  explicit ConsoleTestAccessor(ConsoleClass& c) : _c(c) {}
  bool getBegun() const { return _c._begun; }
  void setBegun(bool v) { _c._begun = v; }
  bool getXoffSent() const { return _c._xoff_sent; }
  void setXoffSent(bool v) { _c._xoff_sent = v; }
  bool isRxBufferEmpty() const { return _c._rx_buffer.empty(); }
  bool isRxBufferFull() const { return _c._rx_buffer.full(); }
  void clearRxBuffer() { _c._rx_buffer.clear(); }
  void pushRxByte(uint8_t b) { _c._rx_buffer.push(b); }
  bool isTxBufferFull() const { return _c._tx_buffer.full(); }
  void clearTxBuffer() { _c._tx_buffer.clear(); }
  void pushTxByte(uint8_t b) { _c._tx_buffer.push_back(b); }
  static ConsoleTestAccessor create(ConsoleClass& c) { return ConsoleTestAccessor(c); }
 private:
  ConsoleClass& _c;
};

#if BRIDGE_ENABLE_DATASTORE
class DataStoreTestAccessor {
 public:
  explicit DataStoreTestAccessor(DataStoreClass& ds) : _ds(ds) {}
  static DataStoreTestAccessor create(DataStoreClass& ds) { return DataStoreTestAccessor(ds); }
 private:
  DataStoreClass& _ds;
};
#endif

#if BRIDGE_ENABLE_PROCESS
class ProcessTestAccessor {
 public:
  explicit ProcessTestAccessor(ProcessClass& p) : _p(p) {}
  bool pushPendingPid(uint16_t pid) { return _p._pushPendingProcessPid(pid); }
  uint16_t popPendingPid() { return _p._popPendingProcessPid(); }
  void clearPendingPids() { _p._pending_process_pids.clear(); }
  static ProcessTestAccessor create(ProcessClass& p) { return ProcessTestAccessor(p); }
 private:
  ProcessClass& _p;
};
#endif

}  // namespace test
}  // namespace bridge

#endif  // BRIDGE_ENABLE_TEST_INTERFACE
#endif
