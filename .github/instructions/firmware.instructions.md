---
applyTo: "**/*.ino,**/*.cpp,**/*.cc,**/*.c,**/*.h,**/*.hpp"
---

Identify the actual board, MCU architecture, core, compiler, memory limits, and build flags before changing firmware. Apply zero-heap/static allocation constraints only where required by that target and existing contract. Prefer installed ETL, Nanopb, wolfSSL, Arduino core, and other appropriate library APIs; do not reimplement existing functionality or add redundant wrappers. Evaluate ISR safety, stack, SRAM, Flash, timing, and peripheral constraints. Choose loops, templates, dispatch tables, and ETL algorithms based on correctness, determinism, readability, and measured resource impact. Run target-relevant builds and emulation where available; distinguish emulation from physical-hardware validation.