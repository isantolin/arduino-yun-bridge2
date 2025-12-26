# Hardcoded 0x Commands Report

The following potentially hardcoded values resembling protocol commands were found in the codebase.
Most are in test files or specific implementations, but some might need to be moved to `spec.toml` if they represent actual protocol constants.

## Arduino Library Tests (`openwrt-library-arduino/tests/`)
- `0x1234` (Mock Command ID in `test_bridge_components.cpp`, `test_protocol.cpp`, `rpc_frame.py`, `test_mailbox_component.py`)
- `0x42AB` (Test Command ID in `test_protocol.cpp`)
- `0x1111` (Test Command ID in `test_protocol.cpp`)
- `0x55AA` (Test Command ID in `test_protocol.cpp`)
- `0x9988` (Test Command ID in `test_protocol.cpp`)
- `0x7F` (Test Exit Code)
- `0xAA`, `0xBB`, `0xCC`, `0xDD`, `0xEE`, `0x5A`, `0x34` (Test Patterns)

## Python Bridge Tests (`openwrt-yun-bridge/tests/`)
- `0xDEADBEEF` (Random Seed/Mock Millis)
- `0x99` (Test Status)

## Arduino Examples
- `openwrt-library-arduino/examples/CorrectedSmokeTest/CorrectedSmokeTest.ino`: `Console.println("Estado: 0x05 (Running)");`
  - `0x05` matches `CMD_SET_BAUDRATE` or `TIMEOUT` status in Spec. Usage here ("Running") conflicts with Spec if referring to a status code.

## Protocol Implementation
- `openwrt-library-arduino/src/protocol/rpc_frame.h`: `PROTOCOL_VERSION = 0x02` (Matches Spec)
- `openwrt-library-arduino/src/protocol/rpc_frame.cpp`: `0xFF`, `0x00` (Masks/Delimiters - Standard)

## Recommendations
1.  Verify if `0x05` in `CorrectedSmokeTest.ino` is intended to be `TIMEOUT` or a different status. If it's a new status "Running", it should be added to `spec.toml` and generated.
2.  Refactor `Bridge.begin(115200)` calls to use `rpc::RPC_DEFAULT_BAUDRATE` generated from the Spec.
