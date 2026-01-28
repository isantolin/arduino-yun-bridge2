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
  size_t getLastRawFrameLen() const { return _bridge._last_raw_frame.size(); }
  
  rpc::FrameParser::Error getLastError() const {
    return _bridge._parser.getError();
  }

  bool isAwaitingAck() const { return _bridge._awaiting_ack; }
  uint16_t getLastCommandId() const { return _bridge._last_command_id; }

  // --- Methods ---
  bool sendControlFrame(uint16_t command_id) {
    return _bridge._sendFrameImmediate(command_id, nullptr, 0);
  }

  void retransmitLastFrame() {
    _bridge._retransmitLastFrame();
  }

  void dispatch(const rpc::Frame& frame) {
    _bridge.dispatch(frame);
  }

  void setSynchronized(bool synchronized) {
    _bridge._synchronized = synchronized;
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