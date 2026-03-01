# Protocol Definitions

This directory contains the auto-generated C++ protocol definitions for the Arduino MCU Bridge v2.

## Source of Truth
The protocol is formally defined in `tools/protocol/spec.toml`. All files in this directory should be considered **read-only artifacts** of the generation process, with the exception of helper classes that wrap the generated structures.

## Files
*   `rpc_protocol.h`: Constants, command IDs, and enums generated from the TOML spec.
*   `rpc_structs.h`: Typed C++ structures for payloads (if using struct-based serialization).
*   `rpc_frame.h`: Frame handling logic (CRC, Header, Payload).

## Generation
To regenerate these files after modifying `spec.toml`:
```bash
python3 tools/protocol/generate.py
```
