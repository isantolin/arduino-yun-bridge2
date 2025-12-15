# Roadmap

## Phase 1: Protocol Analysis & Specification (Completed)
- Defined binary protocol in `tools/protocol/spec.toml`.
- Implemented Python daemon with async architecture.
- Created initial test suite.

## Phase 2: Core Implementation (Completed)
- Implemented full MQTT v5 support.
- Developed robust serial handshake with HMAC authentication.
- Created `openwrt-yun-bridge` and `openwrt-yun-core` packages.

## Phase 3: Execution & Verification (Completed)
- Refactored Arduino C++ library to "Decoupled Bridge" architecture.
- Verified system with comprehensive test suites (`test_bridge_core`, `test_bridge_components`).
- Validated build artifacts and installation scripts.
- Achieved "Ready for Release" status (v2.0.0).

## Future / Ideas
- **Hardware Verification**: Conduct extensive testing on physical Arduino Yun hardware.
- **Performance Tuning**: Optimize serial throughput and memory usage on the MCU.
- **Extended Metrics**: Add more granular metrics for specific subsystems.
