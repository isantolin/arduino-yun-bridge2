# Protocol Code Generation

The files in this directory describe the RPC protocol shared between the MCU
(Arduino) and the MPU (Linux). The canonical definition lives in `mcubridge.proto`
(enums, constants, Cloud topics).
Running the generator updates all derived artifacts to stay in sync.

```bash
python3 tools/protocol/generate.py
```

The command refreshes:

- `mcubridge/mcubridge/protocol/protocol.py` — Python enums and constants from `mcubridge.proto`.
- `mcubridge-library-arduino/src/protocol/rpc_protocol.h` — C++ enums and constants from `mcubridge.proto`.
- `mcubridge-library-arduino/src/protocol/rpc_structs.h` — C++ nanopb type aliases and `Payload::parse<T>` helpers using `pb_decode_noinit` (Nanopb 0.4.9.2).
- `mcubridge/mcubridge/protocol/mcubridge_pb2.py` + `.pyi` — Python protobuf bindings from `mcubridge.proto`.
- `mcubridge-library-arduino/src/protocol/mcubridge.pb.h` + `.pb.c` — C Nanopb 0.4.9.2 structs from `mcubridge.proto`.
- `mcubridge-client-examples/mcubridge_client/protocol.py` — Python client library enums and protocol constants.

The Python module is consumed by the MCU bridge daemon, while the Arduino headers are used by the firmware. Both outputs guarantee zero-heap, zero-copy, and SIL-2 memory safety compliance.
