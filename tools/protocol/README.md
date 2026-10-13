# Protocol Code Generation

The files in this directory describe the RPC protocol shared between the MCU
(Arduino) and the MPU (Linux). The canonical definition lives in `mcubridge.proto`
(messages, enums, constants, runtime configuration defaults, command metadata,
and Cloud topics).
Running the generator updates all derived artifacts to stay in sync.

```bash
python3 tools/protocol/generate.py \
  --spec tools/protocol/mcubridge.proto \
  --py mcubridge/mcubridge/protocol/protocol.py \
  --cpp mcubridge-library-arduino/src/protocol/rpc_protocol.h \
  --cpp-structs mcubridge-library-arduino/src/protocol/rpc_structs.h \
  --py-client mcubridge-client-examples/mcubridge_client/protocol.py
```

The command refreshes:

- `mcubridge/mcubridge/protocol/protocol.py` — Python enums and constants from `mcubridge.proto`.
- `mcubridge-library-arduino/src/protocol/rpc_protocol.h`, `rpc_hw_config.h`, and `rpc_structs.h` — C++ protocol declarations and Nanopb helpers.
- `mcubridge/mcubridge/protocol/mcubridge_pb2.py` + `.pyi` — Python protobuf bindings from `mcubridge.proto`.
- `mcubridge/mcubridge/protocol/mcubridge_grpc.py` — Python gRPC bindings from `mcubridge.proto`.
- `mcubridge-library-arduino/src/protocol/mcubridge.pb.h` + `.pb.c` — C Nanopb structs from `mcubridge.proto`.
- `mcubridge-client-examples/mcubridge_client/protocol.py` — Python client library enums and protocol constants.
- LuCI defaults and schema artifacts rendered from the same protocol metadata.

The Python module is consumed by the MCU bridge daemon, while the Arduino headers are used by the firmware. The firmware bindings are generated for statically bounded Nanopb structures; generation alone does not establish zero-heap behavior on every target or SIL-2 certification.

Nanopb's core version and source-file list are defined in `requirements/runtime.toml`; the dependency synchronizer projects that list into the Arduino installer. Incremental generation fingerprints the schema, Buf configuration and options, generator and templates, dependency manifest, toolchain pin, and installed Buf version. It skips generation only when the fingerprint matches and every expected artifact exists; formatting or generation failures leave the cache uncommitted.
