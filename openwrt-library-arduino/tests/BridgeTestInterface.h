/*
 * This file is part of Arduino MCU Ecosystem v2.
 * (C) 2025-2026 Ignacio Santolin
 *
 * Test interface for BridgeClass - provides controlled access to
 * internal state for unit testing without using #define private public.
 */
#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

#ifdef BRIDGE_ENABLE_TEST_INTERFACE

#include "Bridge.h"

namespace bridge {
namespace test {

/**
 * @brief Test accessor for BridgeClass internals.
 */
class TestAccessor {
 public:
  explicit TestAccessor(BridgeClass& bridge) : _bridge(bridge) {}

  // --- Read-only accessors ---
  
  rpc::FrameParser::Error getLastError() const {
    return _bridge._parser.getError();
  }

  bool isAwaitingAck() const { return _bridge._fsm.isAwaitingAck(); }
  bool isIdle() const { return _bridge._fsm.isIdle(); }
  bool isUnsynchronized() const { return _bridge._fsm.isUnsynchronized(); }
  bool isFault() const { return _bridge._fsm.isFault(); }
  uint16_t getLastCommandId() const { return _bridge._last_command_id; }

  // --- Methods ---
  void retransmitLastFrame() {
    _bridge._retransmitLastFrame();
  }

  void dispatch(const rpc::Frame& frame) {
    _bridge.dispatch(frame);
  }

  // FSM state manipulation for tests
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
    if (synchronized) {
      setIdle();
    } else {
      setUnsynchronized();
    }
  }

  static TestAccessor create(BridgeClass& bridge) {
    return TestAccessor(bridge);
  }

 private:
  BridgeClass& _bridge;
};

}  // namespace test
}  // namespace bridge

#endif  // BRIDGE_ENABLE_TEST_INTERFACE

#endif  // BRIDGE_TEST_INTERFACE_H