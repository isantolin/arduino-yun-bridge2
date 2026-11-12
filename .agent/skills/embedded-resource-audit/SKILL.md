---
name: embedded-resource-audit
description: Analyze Arduino MCU firmware, drivers, ISRs, state machines, serial transport, hardware interfaces, memory, Flash, timing, board builds, or emulation.
---

# Embedded Resource Audit

First identify the actual board, MCU architecture, core, compiler, flags, SRAM/Flash limits, peripheral mapping, and runtime constraints. Never apply AVR assumptions to ESP32 or ARM.

Review allocation, stack use, static buffers, ISR safety, blocking calls, concurrency, FSM transitions, watchdog interaction, and recovery. Use zero-heap/static allocation only where the target and project contract require it. Prefer installed ETL, Nanopb, wolfSSL, Arduino core, and other suitable APIs. Keep adapters only for real hardware/safety/ownership boundaries. Choose loops, templates, algorithms, lookup tables, and dispatch structures based on clarity, determinism, and measured resource cost.

For meaningful firmware changes, inspect the latest Arduino Symbol Profiling artifact or run tools/profiling/arduino_symbol_profiler.py against actual build output. Record before/after SRAM and Flash deltas when comparable. Do not invent WCET bounds; measure them or label estimates.

Choose validation by target: host C++ tests/cppcheck, Arduino example compilation, simavr for supported AVR, Renode for supported ARM, QEMU for supported ESP32/Xtensa, or physical hardware when available. Inspect tox.ini and CI for commands. Report exact board/emulator and limitations. Compilation is not runtime validation; emulation is not a physical-hardware test.