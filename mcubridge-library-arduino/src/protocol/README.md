# Protocol Definitions

This directory contains the auto-generated C++ protocol definitions for the Arduino MCU Bridge v2.

## Source of Truth
The protocol is formally defined in two files at the repository root:
- `tools/protocol/spec.toml` — enums, constants, command IDs, MQTT topics.
- `tools/protocol/mcubridge.proto` — payload message schemas (proto3).

All files in this directory should be considered **read-only artifacts** of the generation process, with the exception of helper classes that wrap the generated structures.

## Files
*   `rpc_protocol.h`: Constants, command IDs, and enums generated from the TOML spec.
*   `rpc_structs.h`: Typed C++ aliases for nanopb payload structs, `REGISTER_DESCRIPTOR` macros, and `Payload::parse<T>()` decoder using `pb_decode`.
*   `rpc_frame.h`: Frame handling logic (CRC, Header, Payload).
*   `mcubridge.pb.h` / `mcubridge.pb.c`: Nanopb-generated structs and field descriptors from `mcubridge.proto`. Static allocation, no heap.

## Generation
To regenerate these files after modifying `spec.toml` or `mcubridge.proto`:
```bash
python3 tools/protocol/generate.py
```
