#!/usr/bin/env bash
# Fail if the AVR port reintroduces a project-owned RPC or transfer stack.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT_ROOT="${ROOT_DIR}/third_party/avr_pigweed_port"
BRIDGE_SOURCE_ROOT="${ROOT_DIR}/mcubridge-library-arduino"

if grep -R -n --include='*.h' --include='*.cpp' --include='*.ino' \
    'BRIDGE_ENABLE_PW_TRANSFER' "$BRIDGE_SOURCE_ROOT"; then
  echo "ERROR: pw_transfer must not have a build-time or runtime enable/disable switch." >&2
  exit 1
fi

if [[ ! -d "$PORT_ROOT" ]]; then
  echo "ERROR: AVR Pigweed port submodule is missing: $PORT_ROOT" >&2
  exit 1
fi

BRIDGE_PIGWEED_ROOT="${ROOT_DIR}/third_party/pigweed"
PORT_PIGWEED_ROOT="${PORT_ROOT}/third_party/pigweed"

for path in "$BRIDGE_PIGWEED_ROOT" "$PORT_PIGWEED_ROOT"; do
  if [[ ! -d "$path" ]]; then
    echo "ERROR: Pigweed submodule is missing: $path" >&2
    exit 1
  fi
done

EXPECTED_PORT_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD:third_party/avr_pigweed_port)"
ACTUAL_PORT_COMMIT="$(git -C "$PORT_ROOT" rev-parse HEAD)"
EXPECTED_BRIDGE_PIGWEED="$(git -C "$ROOT_DIR" rev-parse HEAD:third_party/pigweed)"
ACTUAL_BRIDGE_PIGWEED="$(git -C "$BRIDGE_PIGWEED_ROOT" rev-parse HEAD)"
EXPECTED_PORT_PIGWEED="$(git -C "$PORT_ROOT" rev-parse HEAD:third_party/pigweed)"
ACTUAL_PORT_PIGWEED="$(git -C "$PORT_PIGWEED_ROOT" rev-parse HEAD)"

if [[ "$ACTUAL_PORT_COMMIT" != "$EXPECTED_PORT_COMMIT" ]]; then
  echo "ERROR: AVR port checkout $ACTUAL_PORT_COMMIT differs from Bridge gitlink $EXPECTED_PORT_COMMIT." >&2
  exit 1
fi
if [[ "$ACTUAL_BRIDGE_PIGWEED" != "$EXPECTED_BRIDGE_PIGWEED" ]]; then
  echo "ERROR: Bridge Pigweed checkout $ACTUAL_BRIDGE_PIGWEED differs from its gitlink $EXPECTED_BRIDGE_PIGWEED." >&2
  exit 1
fi
if [[ "$ACTUAL_PORT_PIGWEED" != "$EXPECTED_PORT_PIGWEED" ]]; then
  echo "ERROR: AVR port Pigweed checkout $ACTUAL_PORT_PIGWEED differs from its gitlink $EXPECTED_PORT_PIGWEED." >&2
  exit 1
fi
if [[ "$ACTUAL_BRIDGE_PIGWEED" != "$ACTUAL_PORT_PIGWEED" ]]; then
  echo "ERROR: Pigweed upstream pins diverge: Bridge=$ACTUAL_BRIDGE_PIGWEED AVR-port=$ACTUAL_PORT_PIGWEED." >&2
  exit 1
fi
echo "PASS: Bridge and AVR port share upstream Pigweed $ACTUAL_BRIDGE_PIGWEED."

if [[ -d "$PORT_ROOT/pw_rpc_avr" ]]; then
  echo "ERROR: legacy pw_rpc_avr implementation directory exists." >&2
  exit 1
fi

SCAN_PATHS=()
for path in CMakeLists.txt src tests tools/export_arduino_library.py; do
  if [[ -e "$PORT_ROOT/$path" ]]; then
    SCAN_PATHS+=("$PORT_ROOT/$path")
  fi
done

if grep -R -n -E 'pw_rpc_avr|rpc_dispatch_table\.inc|RpcEnvelope' "${SCAN_PATHS[@]}"; then
  echo "ERROR: project-owned legacy RPC implementation reference found." >&2
  exit 1
fi

if ! grep -R -q 'pw::rpc::Server' "${SCAN_PATHS[@]}"; then
  echo "ERROR: upstream pw_rpc::Server is not referenced by the AVR port." >&2
  exit 1
fi

# The AVR port supplies the upstream pw_transfer module and its cooperative
# execution adaptation; the bridge firmware owns the concrete TransferService.
if ! grep -q 'pw_transfer' "${PORT_ROOT}/CMakeLists.txt"; then
  echo "ERROR: upstream pw_transfer module is not built by the AVR port." >&2
  exit 1
fi

BRIDGE_HEADER="${ROOT_DIR}/mcubridge-library-arduino/src/Bridge.h"
if ! grep -q 'pw::transfer::TransferService' "$BRIDGE_HEADER" ||
   ! grep -q 'pw::transfer::Client' "$BRIDGE_HEADER" ||
   ! grep -q 'pw::transfer::Thread<1, 1>' "$BRIDGE_HEADER"; then
  echo "ERROR: Bridge firmware does not use the upstream pw_transfer service, client, and thread." >&2
  exit 1
fi

echo "PASS: AVR port uses upstream pw_rpc and pw_transfer; no legacy dispatcher remains."
