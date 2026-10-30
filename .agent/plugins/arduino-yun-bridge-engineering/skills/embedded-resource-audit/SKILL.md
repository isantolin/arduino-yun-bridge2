---
name: embedded-resource-audit
description: Analyze Arduino firmware, MCU drivers, ISRs, FSMs, serial transports, board support, SRAM, Flash, stack, and timing budgets.
---

# Embedded Resource Audit

1. Identify exact board, MCU architecture, clock, memory limits, compiler, core/framework, and build flags from current board/build configuration.
2. Do not generalize AVR restrictions to ESP32 or ARM. Inspect current profiler/build reports for the same target and toolchain before making resource claims.
3. Prefer existing ETL, Nanopb, wolfSSL/wolfCrypt, PacketSerial, and board/core APIs when their verified APIs satisfy the contract.
4. Compare alternatives by generated code size, static/dynamic memory, stack usage, execution cost, determinism, and maintainability.
5. Do not replace every loop with an algorithm mechanically. Use ETL/STL-free algorithms when they improve target compatibility, size, or clarity; retain explicit loops when simpler or measurably better.
6. Treat templates as a trade-off: inspect instantiation count, Flash size, code duplication, compiler output, and type safety before choosing template or non-template helpers.
7. Check ISR safety, reentrancy, atomic/volatile semantics, interrupt latency, buffer bounds, object lifetime, watchdog behavior, and safe-state transitions.
8. Do not introduce heap allocation or unbounded buffers where target constraints prohibit them.
9. Do not claim zero-heap, deterministic WCET, functional-safety integrity, or certification without evidence.

## Measurement and validation

Capture a comparable baseline for the same board/compiler/flags, build affected firmware, and inspect SRAM, static data, stack estimates, Flash, and symbol deltas with the project profiler. Run relevant cppcheck/static checks, compile tests, and simavr/QEMU/Renode only where the actual target is supported. Label hardware validation separately from emulation.

## Completion report

State target/toolchain, before/after resource deltas, ISR/FSM/timing considerations, builds, emulation, hardware tests, and remaining risks.
