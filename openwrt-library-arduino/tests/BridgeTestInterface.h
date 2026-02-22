/*
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin
 *
 * Test interface for BridgeClass and subsystem classes - provides
 * controlled access to internal state for unit testing without
 * using the UB-inducing #define private public hack.
 *
 * [E3] All test files should include this header and use the accessor
 *       classes below instead of redefining access specifiers.
 */
#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#ifdef BRIDGE_ENABLE_TEST_INTERFACE

#include "Bridge.h"
#include <string.h>  // memcpy

namespace bridge {
namespace test {

/**
 * @brief Test accessor for BridgeClass internals.
 *
 * Provides read/write access to private state and forwarding of private
 * methods so that tests never need #define private public.
 */
class TestAccessor {
 public:
  explicit TestAccessor(BridgeClass& bridge) : _bridge(bridge) {}

  // ---- FSM state queries (forwarded from public BridgeClass API) ----
  bool isAwaitingAck() const { return _bridge.isAwaitingAck(); }
  bool isIdle() const { return _bridge.isIdle(); }
  bool isUnsynchronized() const { return _bridge.isUnsynchronized(); }
  bool isFault() const { return _bridge.isFault(); }

  // ---- FSM state manipulation (compound helpers) ----
  void setUnsynchronized() { _bridge._fsm.resetFsm(); }
  void setIdle() {
    _bridge._fsm.resetFsm();
    _bridge._fsm.handshakeComplete();
  }
  void setAwaitingAck() {
    setIdle();
    _bridge._fsm.sendCritical();
  }
  void setFault() { _bridge._fsm.cryptoFault(); }
  void setSynchronized(bool synchronized) {
    if (synchronized) { setIdle(); } else { setUnsynchronized(); }
  }

  // ---- FSM primitive operations (when compound helpers are too coarse) ----
  void fsmResetFsm()          { _bridge._fsm.resetFsm(); }
  void fsmHandshakeComplete() { _bridge._fsm.handshakeComplete(); }
  void fsmSendCritical()      { _bridge._fsm.sendCritical(); }
  void fsmCryptoFault()       { _bridge._fsm.cryptoFault(); }

  // ---- Scalar property accessors ----
  uint16_t getLastCommandId() const      { return _bridge._last_command_id; }
  void     setLastCommandId(uint16_t id) { _bridge._last_command_id = id; }

  uint8_t getRetryCount() const        { return _bridge._retry_count; }
  void    setRetryCount(uint8_t count) { _bridge._retry_count = count; }

  uint16_t getAckTimeoutMs() const      { return _bridge._ack_timeout_ms; }
  void     setAckTimeoutMs(uint16_t ms) { _bridge._ack_timeout_ms = ms; }

  uint8_t getAckRetryLimit() const         { return _bridge._ack_retry_limit; }
  void    setAckRetryLimit(uint8_t limit)  { _bridge._ack_retry_limit = limit; }

  uint32_t getResponseTimeoutMs() const      { return _bridge._response_timeout_ms; }
  void     setResponseTimeoutMs(uint32_t ms) { _bridge._response_timeout_ms = ms; }

  uint32_t getLastRxCrc() const        { return _bridge._last_rx_crc; }
  void     setLastRxCrc(uint32_t crc)  { _bridge._last_rx_crc = crc; }

  unsigned long getLastRxCrcMillis() const          { return _bridge._last_rx_crc_millis; }
  void          setLastRxCrcMillis(unsigned long ms) { _bridge._last_rx_crc_millis = ms; }

  bool getStartupStabilizing() const  { return _bridge._startup_stabilizing; }
  void setStartupStabilizing(bool v)  { _bridge._startup_stabilizing = v; }

  // ---- Shared secret ----
  bool   isSharedSecretEmpty() const { return _bridge._shared_secret.empty(); }
  size_t sharedSecretSize()    const { return _bridge._shared_secret.size(); }
  void   assignSharedSecret(const uint8_t* first, const uint8_t* last) {
    _bridge._shared_secret.assign(first, last);
  }
  void clearSharedSecret() { _bridge._shared_secret.clear(); }

  // ---- Pending TX queue ----
  bool isPendingTxQueueFull() const { return _bridge._pending_tx_queue.full(); }
  void clearPendingTxQueue()        { _bridge._pending_tx_queue.clear(); }
  void pushPendingTxFrame(uint16_t command_id, uint16_t payload_length,
                          const uint8_t* payload = nullptr) {
    BridgeClass::PendingTxFrame pf{};
    pf.command_id = command_id;
    pf.payload_length = payload_length;
    if (payload && payload_length > 0) {
      memcpy(pf.payload.data(), payload, payload_length);
    }
    _bridge._pending_tx_queue.push(pf);
  }

  // ---- Parse error ----
  void setLastParseError(rpc::FrameError err)  { _bridge._last_parse_error = err; }
  void clearLastParseError()                    { _bridge._last_parse_error.reset(); }

  // ---- Hardware serial / baudrate ----
  void     setHardwareSerial(HardwareSerial* s) { _bridge._hardware_serial = s; }
  uint32_t getPendingBaudrate() const           { return _bridge._pending_baudrate; }
  void     setPendingBaudrate(uint32_t baud)    { _bridge._pending_baudrate = baud; }

  // ---- Private method forwarders ----
  void dispatch(const rpc::Frame& frame)               { _bridge.dispatch(frame); }
  void retransmitLastFrame()                            { _bridge._retransmitLastFrame(); }
  bool isRecentDuplicateRx(const rpc::Frame& f) const  { return _bridge._isRecentDuplicateRx(f); }
  void markRxProcessed(const rpc::Frame& f)             { _bridge._markRxProcessed(f); }
  void applyTimingConfig(const uint8_t* p, size_t len)  { _bridge._applyTimingConfig(p, len); }
  bool requiresAck(uint16_t cmd) const                  { return _bridge._requiresAck(cmd); }
  void handleAck(uint16_t cmd)                          { _bridge._handleAck(cmd); }
  void handleMalformed(uint16_t cmd)                    { _bridge._handleMalformed(cmd); }
  void handleSystemCommand(const rpc::Frame& f)          { _bridge._handleSystemCommand(f); }
  void handleGpioCommand(const rpc::Frame& f)            { _bridge._handleGpioCommand(f); }
  void computeHandshakeTag(const uint8_t* n, size_t nl,
                           uint8_t* out)                { _bridge._computeHandshakeTag(n, nl, out); }
  void flushPendingTxQueue()                            { _bridge._flushPendingTxQueue(); }

  // ---- ICommandHandler overrides (private in BridgeClass) ----
  void routeStatusCommand(const bridge::router::CommandContext& ctx)    { _bridge.onStatusCommand(ctx); }
  void routeSystemCommand(const bridge::router::CommandContext& ctx)    { _bridge.onSystemCommand(ctx); }
  void routeGpioCommand(const bridge::router::CommandContext& ctx)      { _bridge.onGpioCommand(ctx); }
  void routeConsoleCommand(const bridge::router::CommandContext& ctx)   { _bridge.onConsoleCommand(ctx); }
  void routeDataStoreCommand(const bridge::router::CommandContext& ctx) { _bridge.onDataStoreCommand(ctx); }
  void routeMailboxCommand(const bridge::router::CommandContext& ctx)   { _bridge.onMailboxCommand(ctx); }
  void routeFileSystemCommand(const bridge::router::CommandContext& ctx){ _bridge.onFileSystemCommand(ctx); }
  void routeProcessCommand(const bridge::router::CommandContext& ctx)   { _bridge.onProcessCommand(ctx); }
  void routeUnknownCommand(const bridge::router::CommandContext& ctx)   { _bridge.onUnknownCommand(ctx); }

  static TestAccessor create(BridgeClass& bridge) { return TestAccessor(bridge); }

 private:
  BridgeClass& _bridge;
};

// ---------------------------------------------------------------------------

/**
 * @brief Test accessor for ConsoleClass internals.
 */
class ConsoleTestAccessor {
 public:
  explicit ConsoleTestAccessor(ConsoleClass& c) : _c(c) {}

  bool getBegun() const        { return _c._begun; }
  void setBegun(bool v)        { _c._begun = v; }

  bool getXoffSent() const     { return _c._xoff_sent; }
  void setXoffSent(bool v)     { _c._xoff_sent = v; }

  // RX buffer
  bool isRxBufferEmpty() const { return _c._rx_buffer.empty(); }
  bool isRxBufferFull()  const { return _c._rx_buffer.full(); }
  void clearRxBuffer()         { _c._rx_buffer.clear(); }
  void pushRxByte(uint8_t b)   { _c._rx_buffer.push(b); }

  // TX buffer
  bool isTxBufferFull() const  { return _c._tx_buffer.full(); }
  void clearTxBuffer()         { _c._tx_buffer.clear(); }
  void pushTxByte(uint8_t b)   { _c._tx_buffer.push_back(b); }

  static ConsoleTestAccessor create(ConsoleClass& c) { return ConsoleTestAccessor(c); }

 private:
  ConsoleClass& _c;
};

// ---------------------------------------------------------------------------

#if BRIDGE_ENABLE_DATASTORE
/**
 * @brief Test accessor for DataStoreClass internals.
 */
class DataStoreTestAccessor {
 public:
  explicit DataStoreTestAccessor(DataStoreClass& ds) : _ds(ds) {}

  bool        trackPendingKey(const char* key) { return _ds._trackPendingDatastoreKey(key); }
  const char* popPendingKey()                  { return _ds._popPendingDatastoreKey(); }
  void        clearPendingKeys()               { _ds._pending_datastore_keys.clear(); }

  static DataStoreTestAccessor create(DataStoreClass& ds) { return DataStoreTestAccessor(ds); }

 private:
  DataStoreClass& _ds;
};
#endif

// ---------------------------------------------------------------------------

#if BRIDGE_ENABLE_PROCESS
/**
 * @brief Test accessor for ProcessClass internals.
 */
class ProcessTestAccessor {
 public:
  explicit ProcessTestAccessor(ProcessClass& p) : _p(p) {}

  bool     pushPendingPid(uint16_t pid) { return _p._pushPendingProcessPid(pid); }
  uint16_t popPendingPid()              { return _p._popPendingProcessPid(); }
  void     clearPendingPids()           { _p._pending_process_pids.clear(); }

  static ProcessTestAccessor create(ProcessClass& p) { return ProcessTestAccessor(p); }

 private:
  ProcessClass& _p;
};
#endif

}  // namespace test
}  // namespace bridge

#endif  // BRIDGE_ENABLE_TEST_INTERFACE

#endif  // BRIDGE_TEST_INTERFACE_H