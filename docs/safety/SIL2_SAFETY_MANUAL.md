# Functional Safety Design Notes: Arduino MCU Bridge 2

**Standards referenced:** IEC 61508, IEC 62304, ISO 26262
**System Version:** v2.8.5+  
**Target Hardware:** Arduino Microcontroller (MCU) $\leftrightarrow$ Linux Microprocessor (MPU)

**Evidence status:** This repository documents design mechanisms and project test results; it does not contain a safety case or establish IEC 61508 SIL-2, IEC 62304, or ISO 26262 certification. Such claims require target-specific evidence and independent assessment.

---

## 1. System Safety Overview

Arduino MCU Bridge 2 implements deterministic communication mechanisms for Arduino-compatible microcontrollers and Linux-based microprocessors. The mechanisms below are design notes, not a claim of certified functional-safety compliance.

```mermaid
flowchart TD
    subgraph Power-On Phase
        A[Hardware Boot] --> B[POST: SRAM March Test]
        B --> C[POST: Stack Sentinel Initialization]
        C --> D[Cryptographic self-tests (KATs)]
        D -- Fail --> E[Safe State: FAULT / Pins Disabled]
        D -- Pass --> F[FSM Started / Unsynchronized]
    end

    subgraph Operational Phase (100 Hz Loop)
        F --> G[Bridge::process]
        G --> H{Stack Sentinel Check}
        H -- Overflow/Corrupt --> E
        H -- Intact --> I[Execute Tasks / Watchdog Kick]
        I --> J[Observed Process-Time Tracking]
        J --> G
    end
```

---

## 2. Core Architectural Safety Measures

### 2.1 Zero-Heap Memory Allocation
* **Static Memory Guarantee**: Strictly 100% of memory allocations on the MCU are static (`etl::array`, `etl::vector`, static queues).
* **Zero Dynamic Allocation**: `malloc()`, `free()`, `new`, `delete`, and STL heap-based containers (`std::vector`, `std::string`) are strictly prohibited in production code.
* **Elimination of Memory Hazards**: Zero heap fragmentation, zero memory leaks, deterministic O(1) buffer access.

### 2.2 Deterministic Finite State Machine (FSM)
* **Strongly-Typed States**: Implemented via `etl::fsm` using `enum class StateId : uint8_t` (e.g. `UNSYNCHRONIZED`, `SYNCHRONIZED`, `FAULT`).
* **Explicit Transitions**: Supported transitions are encoded per state. Some unhandled events remain in the current state; their safety behavior must be verified for each event/state pair.

### 2.3 Power-On Self-Test (POST)
At system startup (`Bridge.begin()`), the HAL executes `bridge::hal::run_power_on_self_tests()`:
1. **SRAM March Pattern Test**: Non-destructive pattern verification (`0x55`, `0xAA`, `0x00`, `0xFF`) verifying memory cell integrity.
2. **Stack Sentinel Initialization**: Sets the stack boundary canary (`0x55AA55AA`).
3. **Cryptographic Self-Tests**: MCU KATs cover SHA-256, HMAC-SHA256, and ChaCha20-Poly1305. The daemon also checks its ChaCha20-Poly1305 library and exercises encrypted transport frames.
4. **Safety Action**: If any POST step fails, `enterSafeState()` is invoked immediately, disabling transmitters and driving I/O pins to a safe state.

### 2.4 Continuous Stack Overflow Protection (Watermarking)
* **Sentinel Canary**: A 32-bit sentinel (`STACK_CANARY_VALUE = 0x55AA55AA`) is placed at the boundary between static memory and the stack.
* **Runtime Verification**: On every execution cycle of `Bridge.process()`, `bridge::hal::checkStackOverflow()` verifies that:
  - The stack canary has not been overwritten.
  - Free stack margin is $\ge$ `MIN_STACK_MARGIN_BYTES` (64 bytes on AVR).
* If corrupted, the system trips a safety interlock and enters `FAULT`.

### 2.5 Runtime Execution-Time Monitoring (Observed Maximum, Not WCET Proof)
* Each `BridgeClass::process()` invocation measures elapsed time with `micros()` and updates `_wcet_max_micros`, exposed through `Bridge.getWcetMaxMicros()`.
* This is the maximum observed duration of calls to `Bridge.process()` during a run. It does not measure the caller's full loop or operations outside that call, such as application sensor reads and `Bridge.sendPinEvent()`.
* An observed maximum is not a proven worst-case execution-time bound and does not guarantee that communication tasks cannot starve application work. A WCET claim requires target-specific bounds for execution paths, interrupts, and serial workloads, plus validation on the intended hardware. Host benchmarks and functional simavr runs alone do not establish that bound.

### 2.6 Hardware Watchdog Integration
* **Watchdog Supervision**: Hardware watchdog timer enabled at 4-second timeout (`wdt_enable(WDTO_4S)` on AVR, `esp_task_wdt` on ESP32, and Procd watchdog in OpenWrt).
* **Heartbeat Refreshes**: The watchdog is kicked solely if the FSM is alive and memory integrity passes.

### 2.7 Zero-Copy Static Deserialization (Nanopb 0.4.9.2)
* **`pb_decode_noinit` Semantics**: Payloads crossing the RPC boundary are decoded directly into pre-allocated static structs without memory re-zeroing (`memset`), preventing CPU cycle exhaustion and stack thrashing on resource-constrained microcontrollers.
* **Deterministic Bounds**: All variable-length fields are statically constrained via `.options` file definitions.

### 2.8 Link Authentication and Disconnect Handling
* With a configured shared secret, `LinkSync` verifies host and MCU HMAC proofs over a 16-byte host nonce. An empty secret explicitly disables authentication.
* With a shared secret, RPC, console, and log frames are protected by ChaCha20-Poly1305 and monotonic per-direction counters within a session. CRC-32 remains error detection, not authentication.
* Counter state is not persistent across MCU restarts; replay of a previously captured authenticated handshake and its frames is not ruled out.
* The explicit `LinkReset` control request is plaintext and can force a new handshake; this is not a plaintext data-frame fallback.
* Disconnects transition the link to `UNSYNCHRONIZED`, clear pending handshake expectations, and require a new synchronization.

### 2.9 State Machines in Linux MPU (`python-statemachine`)
* **Explicit Lifecycle States**: Handshake (`HandshakeMachine`), physical link (`LinkConnectionMachine`), subprocesses (`ProcessMachine`), and cloud sessions (`GatewaySessionMachine`) use discrete state machines. Their presence alone does not establish SIL-2 determinism or certification.
* **Native Listener Hook Dispatch**: State side effects (e.g. enabling `serial_tx_allowed`, setting/clearing `link_sync_event`, escalating process termination) are executed strictly via synchronous, typed listener callbacks (`on_enter_*`, `on_exit_*`), eradicating manual glue code and race-prone polling.
* **Closed Transition Matrices & Concurrency Safety**: Illegal transitions are rejected deterministically; idempotent transitions and `allow_event_without_transition = True` prevent deadlock or unhandled exception propagation during asynchronous subprocess termination and reconnection events.

### 2.10 Google Pigweed Framing, RPC Dispatch, and Atomic Timekeeping (`pw_hdlc`, `pw_rpc`, `pw_chrono`)
* **Multi-Channel HDLC Framing (`pw_hdlc`)**: All serial frames use `pw::hdlc::DecoderBuffer` and `pw::hdlc::EncodeUIFrame` (`pw_hdlc`) with IEEE 802.3 CRC-32 FCS verification, isolating `pw_rpc` control packets (`HDLC_ADDRESS_RPC = 1`), tokenized diagnostics (`HDLC_ADDRESS_LOG = 2`), and console streams (`HDLC_ADDRESS_CONSOLE = 3`).
* **Compile-Time FNV-1a Service & Client Dispatch (`pw::rpc::ClientServer`)**: The MCU registers 6 static Nanopb server services (`rpc::pb::pw_rpc::nanopb::<Service>::Service<Impl>`) and invokes MCU-to-MPU RPCs via generated Nanopb clients (`rpc::pb::pw_rpc::nanopb::<Service>::Client` with `pw::rpc::NanopbUnaryReceiver` and `pw::rpc::NanopbServerWriter`) over `pw::rpc::Channel` using `constexpr` `pw::tokenizer::Hash("rpc.pb.<Service>")` and `pw::tokenizer::Hash("<Method>")` identifiers without dynamic allocation or STL dependencies (`-DETL_NO_STL`).
* **Atomic Millisecond Timekeeping & Tokenized Logging (`pw::chrono::SystemClock`, `pw::log::SetLogSink`)**: `BridgeClass` uses `pw::chrono::SystemClock::now()` (backed by `pw_chrono` with 1 ms tick resolution) for atomic 32-bit millisecond timestamps across watchdog supervision, frame timeouts, handshake deadlines, and periodic pin sampling, and routes tokenized logs through `pw::log::SetLogSink`.

---

## 3. Project-Reported Safety and Test Metrics

The figures below are historical project-reported values. This manual does not identify the originating run, commit, board configuration, or calculation for every metric. They must be reproduced for the exact target and must not be treated as certification evidence.

| Metric | Reference value | Project-reported value | Evidence status |
| :--- | :--- | :--- | :--- |
| **Safe Failure Fraction (SFF)** | Requires a documented safety analysis | $> 99.2\%$ | Calculation/evidence not included here |
| **Dynamic Memory Allocation** | Target-specific property | 0 bytes (Zero-Heap) | Verify for each firmware configuration |
| **Test Statement Coverage** | Test metric, not a certification threshold | **98.8%** (C++) / **98.4%** (Python) | Reproduce from the matching test run |
| **Test Branch Coverage** | Test metric, not a certification threshold | **95.7%** (C++) / **95.2%** (Python) | Reproduce from the matching test run |
| **Error Suppressions** | Audit policy | 0 suppressions | Verify with the current audit run |
