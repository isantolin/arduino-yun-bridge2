# Protocol Code Generation

The files in this directory describe the RPC protocol shared between the MCU
(Arduino) and the MPU (Linux). The canonical definitions are modularized across:
- `config_schema.proto`: system constants, custom options, enums, runtime configuration.
- `mcu_wire.proto`: physical wire frames, RPC envelopes, and bounded static payloads.
- `cloud_gateway.proto`: telemetry reports, cloud envelopes, and gRPC services.

Running the generator updates all derived artifacts. MCU dispatch handlers live next to their command/status declarations in `config_schema.proto`; no parallel handler table is maintained.

```bash
python3 tools/protocol/generate.py \
  --spec tools/protocol/cloud_gateway.proto \
  --py mcubridge/mcubridge/protocol/protocol.py \
  --cpp mcubridge-library-arduino/src/protocol/rpc_protocol.h \
  --cpp-structs mcubridge-library-arduino/src/protocol/rpc_structs.h \
  --py-client mcubridge-client-examples/mcubridge_client/protocol.py
```

The command refreshes:

- `mcubridge/mcubridge/protocol/protocol.py` — Python enums and constants from the schema.
- `mcubridge-library-arduino/src/protocol/rpc_protocol.h`, `rpc_hw_config.h`, `rpc_structs.h`, and `rpc_dispatch_table.inc` — C++ protocol declarations, Nanopb helpers, and the generated static dispatch table.
- `mcubridge/mcubridge/protocol/mcubridge_pb2.py` + `.pyi` — Python protobuf bindings.
- `mcubridge/mcubridge/protocol/mcubridge_grpc.py` — Python gRPC bindings.
- `mcubridge-library-arduino/src/protocol/mcu_wire.pb.h` + `config_schema.pb.h` — C Nanopb structs for MCU.
- `mcubridge-client-examples/mcubridge_client/protocol.py` — Python client library enums and protocol constants.
- LuCI defaults and schema artifacts rendered from the same protocol metadata.

The Python module is consumed by the MCU bridge daemon, while the Arduino headers are used by the firmware. The firmware bindings are generated for statically bounded Nanopb structures; generation alone does not establish zero-heap behavior on every target or SIL-2 certification.

Nanopb's runtime core version and source-file list are defined in `requirements/runtime.toml`; `tools/audit/sync_runtime_deps.py` projects that list into `mcubridge-library-arduino/tools/install.sh`, which downloads the core files. Protocol generation no longer downloads runtime C files or rewrites source formatting. Run `buf format -w tools/protocol` explicitly when formatting is needed; CI checks formatting separately. Package version synchronization is integrated into `python3 tools/audit/sync_runtime_deps.py`. Incremental generation fingerprints the schema, Buf configuration and options, generator and templates, toolchain pin, and installed Buf version. It skips generation only when the fingerprint matches and every expected artifact exists; generation failures leave the cache uncommitted.
