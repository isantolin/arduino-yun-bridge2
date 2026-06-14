# Protocol Definitions

This directory contains the auto-generated C++ protocol definitions for the Arduino MCU Bridge v2.

## Source of Truth
The protocol is formally defined in two files at the repository root:
- `tools/protocol/mcubridge.proto` — enums, constants, command IDs, MQTT topics.
- `tools/protocol/mcubridge.proto` — payload message schemas (proto3).

All files in this directory should be considered **read-only artifacts** of the generation process, with the exception of helper classes that wrap the generated structures.

## Files
*   `rpc_protocol.h`: Constants, command IDs, and enums generated from the TOML spec.
*   `rpc_structs.h`: Native C++ payload structs with direct Mpack `encode()`/`decode()` methods and `Payload::parse<T>()` decoder.
*   `rpc_frame.h`: Frame handling logic (CRC, Header, Payload).

## Generation
To regenerate these files after modifying `mcubridge.proto`:
```bash
python3 tools/protocol/generate.py
```
