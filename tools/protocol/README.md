# Protocol Code Generation

The files in this directory describe the RPC protocol shared between the MCU
(Arduino) and the MPU (Linux). The canonical definition lives in `spec.toml`.
Running the generator updates all derived artifacts to stay in sync.

```bash
python3 tools/protocol/generate.py
```

The command refreshes:

- `openwrt-yun-bridge/yunbridge/rpc/protocol.py`
- `openwrt-library-arduino/src/protocol/rpc_protocol.h`

The Python module is consumed by the Yun bridge daemon, while the Arduino
header is used by the firmware. Both outputs embed the license header from the
spec so there is a single point of maintenance.

Consider wiring the generator into CI or the OpenWrt build pipeline to ensure
protocol drift is caught early.
