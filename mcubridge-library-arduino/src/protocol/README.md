# Protocol Definitions

This directory contains the auto-generated C++ protocol definitions for the Arduino MCU Bridge v2.

## Source of Truth
The protocol is formally defined in modular proto3 files under `tools/protocol/`:
- `config_schema.proto` & `mcu_wire.proto` — enums, constants, command IDs, and wire frame schemas.
- Generated nanopb bindings: `config_schema.pb.h`/`.c` and `mcu_wire.pb.h`/`.c` — C structs for all MCU messages (no heap, static sizing).

All files in this directory should be considered **read-only artifacts** of the generation process, with the exception of helper classes that wrap the generated structures.

## Files
*   `rpc_protocol.h`: Constants, command IDs, and enums generated from the protobuf spec.
*   `rpc_structs.h`: Native C++ payload structs with direct Nanopb `encode()`/`decode()` methods and `Payload::parse<T>()` decoder.
*   `rpc_frame.h`: Frame handling logic (CRC, Header, Payload).

## Generation
To regenerate these files after modifying `mcubridge.proto`:
```bash
python3 tools/protocol/generate.py
```
