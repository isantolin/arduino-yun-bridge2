#!/usr/bin/env bash
# Fail if the AVR port reintroduces a project-owned RPC or transfer stack.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT_ROOT="${ROOT_DIR}/third_party/avr_pigweed_port"

if [[ ! -d "$PORT_ROOT" ]]; then
  echo "ERROR: AVR Pigweed port submodule is missing: $PORT_ROOT" >&2
  exit 1
fi

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

if grep -R -n -E 'pw_rpc_avr|rpc_dispatch_table\\.inc|RpcEnvelope' "${SCAN_PATHS[@]}"; then
  echo "ERROR: project-owned legacy RPC implementation reference found." >&2
  exit 1
fi

if ! grep -R -q 'pw::rpc::Server' "${SCAN_PATHS[@]}"; then
  echo "ERROR: upstream pw_rpc::Server is not referenced by the AVR port." >&2
  exit 1
fi

if ! grep -R -q 'pw_transfer::TransferService' "${SCAN_PATHS[@]}"; then
  echo "ERROR: upstream pw_transfer::TransferService is not referenced by the AVR port." >&2
  exit 1
fi

echo "PASS: AVR port uses upstream pw_rpc and pw_transfer; no legacy dispatcher remains."
