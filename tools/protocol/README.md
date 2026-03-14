# Protocol Code Generation

The files in this directory describe the RPC protocol shared between the MCU
(Arduino) and the MPU (Linux). The canonical definition lives in `spec.toml`
(enums, constants, MQTT topics) and `mcubridge.proto` (payload schemas).
Running the generator updates all derived artifacts to stay in sync.

```bash
python3 tools/protocol/generate.py
```

The command refreshes:

- `mcubridge/mcubridge/protocol/protocol.py` — Python enums and constants from `spec.toml`.
- `mcubridge-library-arduino/src/protocol/rpc_protocol.h` — C++ enums and constants from `spec.toml`.
- `mcubridge-library-arduino/src/protocol/rpc_structs.h` — C++ nanopb type aliases and `Payload::parse<T>` wrappers.
- `mcubridge/mcubridge/protocol/mcubridge_pb2.py` + `.pyi` — Python protobuf bindings from `mcubridge.proto`.
- `mcubridge-library-arduino/src/protocol/mcubridge.pb.h` + `.pb.c` — C nanopb structs from `mcubridge.proto`.

The Python module is consumed by the MCU bridge daemon, while the Arduino
headers are used by the firmware. Both outputs embed the license header from the
spec so there is a single point of maintenance.

Consider wiring the generator into CI or the OpenWrt build pipeline to ensure
protocol drift is caught early.
