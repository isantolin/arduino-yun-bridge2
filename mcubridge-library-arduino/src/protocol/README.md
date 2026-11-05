# Protocol Definitions

This directory contains the auto-generated C++ protocol definitions for the Arduino MCU Bridge v2.

## Source of Truth
The protocol is formally defined in modular proto3 files under `tools/protocol/`:
- `config_schema.proto` & `mcu_wire.proto` — enums, constants, command IDs, and wire frame schemas.
- Generated nanopb bindings: `config_schema.pb.h`/`.c` and `mcu_wire.pb.h`/`.c` — C structs for all MCU messages (no heap, static sizing).

All files in this directory should be considered **read-only artifacts** of the generation process, with the exception of helper classes that wrap the generated structures.

## Files
*   `rpc_protocol.h`: Constants, command IDs, enums, and `rpc::encode_pb` / `rpc::decode_pb` Nanopb helpers generated from the protobuf spec.
*   `rpc_hw_config.h`: Hardware buffer sizes and limits generated from `config_schema.proto`.
*   `mcu_wire.pb.h` / `mcu_wire.pb.c` & `config_schema.pb.h` / `config_schema.pb.c`: Zero-heap Nanopb C structs and descriptors.

## Generation
To regenerate these files after modifying the `.proto` schemas under `tools/protocol/`:
```bash
python3 tools/protocol/generate.py \
  --spec tools/protocol/cloud_gateway.proto \
  --py mcubridge/mcubridge/protocol/protocol.py \
  --cpp mcubridge-library-arduino/src/protocol/rpc_protocol.h \
  --py-client mcubridge-client-examples/mcubridge_client/protocol.py
```
