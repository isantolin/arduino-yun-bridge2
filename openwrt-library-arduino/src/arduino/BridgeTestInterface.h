/*
 * This file is part of Arduino Yun Ecosystem v2.
 * (C) 2025 Ignacio Santolin
 *
 * Test interface for BridgeTransport - provides controlled access to
 * internal state for unit testing without using #define private public.
 *
 * [FIRMWARE OPTIMIZATION] This file is EXCLUDED from production firmware.
 * It is only compiled when BRIDGE_ENABLE_TEST_INTERFACE is defined,
 * which should only happen in host test builds (not Arduino sketches).
 * This saves ~500 bytes of Flash in production builds.
 */
#ifndef BRIDGE_TEST_INTERFACE_H
#define BRIDGE_TEST_INTERFACE_H

// [GUARD] Only compile test interface when explicitly enabled for host tests
#ifdef BRIDGE_ENABLE_TEST_INTERFACE

#include "BridgeTransport.h"

namespace bridge {
namespace test {

/**
 * @brief Test accessor for BridgeTransport internals.
 *
 * This class provides controlled access to BridgeTransport private members
 * for unit testing purposes. It uses friend class pattern instead of the
 * problematic `#define private public` anti-pattern.
 *
 * Usage:
 *   BridgeTransport transport(...);
 *   auto accessor = TestAccessor::create(transport);
 *   size_t len = accessor.getLastCobsLen();
 */
class TestAccessor {
 public:
  explicit TestAccessor(BridgeTransport& transport) : _transport(transport) {}

  // --- Read-only accessors for test assertions ---
  size_t getLastCobsLen() const { return _transport._last_cobs_len; }
  bool isFlowPaused() const { return _transport._flow_paused; }
  bool hasOverflowed() const { return _transport._parser.overflowed(); }

  rpc::FrameParser::Error getLastError() const {
    return _transport._parser.getError();
  }

  // --- Factory method ---
  static TestAccessor create(BridgeTransport& transport) {
    return TestAccessor(transport);
  }

 private:
  BridgeTransport& _transport;
};

}  // namespace test
}  // namespace bridge

#endif  // BRIDGE_ENABLE_TEST_INTERFACE

#endif  // BRIDGE_TEST_INTERFACE_H
