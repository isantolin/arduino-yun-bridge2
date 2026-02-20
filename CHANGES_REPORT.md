# Changes Report

## C++ Firmware (SIL-2 Compliant)

### 1. Hierarchical FSM (`bridge_fsm.h`)
- **Refactoring:** Introduced `StateSynchronized` as a parent state for `StateIdle` and `StateAwaitingAck`.
- **Benefit:** Centralized handling of common events like `EvReset` and `EvCryptoFault` in the parent state, simplifying the logic in leaf states.
- **Compliance:** Maintains SIL-2 deterministic behavior (static allocation, bounded transitions).

### 2. Observer Pattern (`Bridge.h`, `Bridge.cpp`)
- **Implementation:** Added `etl::observer` support.
- **Interface:** Defined `BridgeObserver` with virtual methods:
    - `onBridgeSynchronized()`
    - `onBridgeLost()`
    - `onBridgeError(rpc::StatusCode)`
- **Integration:** `BridgeClass` now inherits from `etl::observable`.
- **Notifications:** Added `notify_observers` calls at critical state transitions (Handshake success, Safe State entry, Error emission).

## Python Daemon

### 3. MessagePack Spooling (`spool.py`, `messages.py`)
- **Optimization:** Switched MQTT spool storage from JSON to **MessagePack** (`msgspec.msgpack`).
- **Benefit:** 
    - Improved performance (faster encoding/decoding).
    - Reduced disk usage.
    - Native byte string support (removed base64 overhead).
- **Refactoring:** Updated `QueuedPublish` and `SpoolRecord` to handle `bytes` payloads directly.

### 4. BitStruct Protocol Optimization (`structures.py`, `context.py`)
- **Implementation:** Replaced `Int32ub` bitmask for `CapabilitiesPacket` features with `construct.BitStruct`.
- **Structure:** Defined `CapabilitiesFeatures` typed struct for parsed flags.
- **Breaking Change:** Python internal representation of capabilities is now a nested object, not a flat integer. Binary format remains compatible on the wire but requires updated parsing logic.
- **Type Safety:** Updated `BaseStruct.decode` to use `msgspec.convert` for robust Construct-to-Msgspec conversion, and fixed runtime type introspection issues with `construct.Construct`.

### 5. Reliability (`handshake.py`, `daemon.py`)
- **Verification:** Confirmed usage of `tenacity.stop_after_attempt` in critical retry loops (`synchronize`, `supervise_task`).

## Testing
- **Python:** Updated tests to reflect MessagePack usage and `BitStruct` capabilities (passing dict of flags instead of int).
- **Status:** **703/703 tests passed**.
