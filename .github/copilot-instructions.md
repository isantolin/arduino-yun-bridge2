# Arduino Yun Bridge 2 Copilot Instructions

## Quick Context
- Modern replacement for the legacy Yun Bridge: Python daemon on the MPU, C++ library on the MCU, MQTT v5 everywhere for async RPC.
- Mission-critical handshake: MCU and Linux exchange HMAC-authenticated frames before MQTT services come online; never ship with the placeholder `changeme123` secret.
- Releases must keep protocol artifacts in sync between `openwrt-yun-bridge/yunbridge/rpc/` and `openwrt-library-arduino/src/protocol/`.

## Architecture & Source Layout
- `openwrt-yun-bridge/`: async daemon (`yunbridge/daemon.py`, `BridgeService`, `RuntimeState`, MQTT helpers), init scripts, and Python tests.
- `openwrt-library-arduino/`: MCU runtime, sketches under `examples/`, protocol glue under `src/protocol/` (COBS, CRC, RPC enums).
- `luci-app-yunbridge/`: LuCI UI plus CGI endpoints that mirror `/tmp/yunbridge_status.json` and `br/system/status`.
- `openwrt-yun-examples-python/`: MQTT client scripts that reuse the daemon's DTO modules (`yunbridge.mqtt.messages` + `yunbridge.mqtt.inbound`) while talking to `aiomqtt` directly; useful for manual verification.
- `feeds/` + `openwrt-sdk/`: local OpenWrt feed populated by `tools/sync_feed_overlay.sh`; SDK builds APKs via the top-level scripts.

## Protocol, Config, and Secrets
- Edit `tools/protocol/spec.toml` then run `python3 tools/protocol/generate.py` to refresh both Python (`yunbridge/rpc/protocol.py`) and C++ (`openwrt-library-arduino/src/protocol/`). Commit both sides together.
- Runtime defaults live in `openwrt-yun-bridge/yunbridge/const.py`; adjust values there and expose overrides via UCI (`yunbridge.general.*`).
- Rotate serial + MQTT credentials with `tools/rotate_credentials.sh --host <yun>` or `/usr/bin/yunbridge-rotate-credentials` so sketches can embed `#define BRIDGE_SERIAL_SHARED_SECRET "..."`.
- Update topic ACLs (`mqtt_allow_*`, `allowed_commands`) in both policy code (`yunbridge/policy.py`) and LuCI defaults whenever permissions change.

## Build, Install, and Dependencies
- `./1_compile.sh [openwrt-version] [target]` downloads the SDK, refreshes `feeds/yunbridge`, and builds local APKs for all dependencies.
- `./2_expand.sh` prepares storage (extroot/overlay); `./3_install.sh` deploys packages from `bin/` using `apk add`.
- After editing `requirements/runtime.toml`, run `python3 tools/sync_runtime_deps.py` (or `--check` in CI) to update `requirements/runtime.txt` and Makefiles.
- Local parity: `pip install -r requirements/runtime.txt` (Python 3.13) matches the daemon environment.

## Testing & Quality Gates
- Python unit tests: `tox -e py313 -- --maxfail=1 --durations=10`; Pyright config sits at repo root for static checks.
- Coverage stack: `./tools/coverage_python.sh` + `./tools/coverage_arduino.sh`, or `tox -e coverage` followed by `python tools/coverage_report.py` to refresh `coverage/coverage-summary.md`.
- Hardware smoke tests: `./tools/hardware_smoke_test.sh --host <yun>` for a single board; `./tools/hardware_harness.py --manifest hardware/targets.toml --max-parallel 3 --tag regression` to fan out and emit JSON reports.
- Manual MQTT verification: run scripts in `openwrt-yun-examples-python/` against the broker at `127.0.0.1:8883`; ensure they see MQTT v5 `response_topic` + `correlation_data`.

## Coding & Operational Conventions
- Logs are structured JSON (`ts`, `level`, `logger`, `message`, `extra`). Preserve keys and only append fields that downstream tooling understands.
- `RuntimeState` feeds `/tmp/yunbridge_status.json`, `br/system/status`, `br/system/metrics`, and the optional Prometheus exporter (`metrics_enabled`). Keep these snapshots consistent when adding fields.
- Serial link is single-threaded: honor `pending_pin_request_limit`, mailbox/console queue caps, and MQTT queue thresholds before enqueueing new work.
- Shell/process requests must pass through `AllowedCommandPolicy`; update LuCI text and docs whenever the whitelist schema changes.
- MCU no longer initiates pin reads (Linux daemon exclusively originates `CMD_DIGITAL_READ`/`CMD_ANALOG_READ`). Any MCU protocol tweak requires a corresponding daemon handler update.

## Handy References
- `docs/PROTOCOL.md`: contrato del protocolo + deep dive on BridgeService, RuntimeState, metrics, and security controls.
- `docs/CREDENTIALS.md`: TLS bundle rollout, watchdog tuning, respawn guidance.
- Logs: `logread` (syslog ring buffer in RAM) + `/tmp/yunbridge_status.json`: primary debug artifacts; replicate scenarios in tests where possible.

Keep edits concise and contextual: reference the exact script or module you touch, tie it back to the MQTT/OpenWrt workflow, and ask for clarification if a workflow is unclear.
