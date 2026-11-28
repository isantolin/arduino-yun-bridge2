#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

COVERAGE_MODE=false
INSTALL_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --coverage) COVERAGE_MODE=true ;;
        --install-only) INSTALL_ONLY=true ;;
    esac
done

# Protocol artifacts remain generated from the canonical .proto inputs.
python3 tools/protocol/generate.py \
    --spec tools/protocol/cloud_gateway.proto \
    --py mcubridge/mcubridge/protocol/protocol.py \
    --cpp mcubridge-library-arduino/src/protocol/rpc_protocol.h \
    --py-client mcubridge-client-examples/mcubridge_client/protocol.py

if [ "$INSTALL_ONLY" = true ]; then
    ARDUINO_LIBS="${ARDUINO_LIB_DIR:-$HOME/Arduino/libraries}"
    mkdir -p "${ARDUINO_LIBS}"
    mcubridge-library-arduino/tools/install.sh "${ARDUINO_LIBS}"
    exit 0
fi

if ! command -v bazel >/dev/null 2>&1; then
    echo "[host-cpp] ERROR: Bazel/Bazelisk is required for Pigweed pw_unit_test." >&2
    exit 127
fi

TEST_TARGETS=(
    //mcubridge-library-arduino:test_integrated
    //mcubridge-library-arduino:test_bridge_core
    //mcubridge-library-arduino:test_bridge_components
    //mcubridge-library-arduino:test_host_filesystem
    //mcubridge-library-arduino:test_protocol
    //mcubridge-library-arduino:test_fsm_mutual_auth
    //mcubridge-library-arduino:test_rpc_structs
    //mcubridge-library-arduino:test_arduino_harden
    //mcubridge-library-arduino:test_arduino_crypto_harden
    //mcubridge-library-arduino:test_arduino_stress
    //mcubridge-library-arduino:test_bridge_edge_paths
    //mcubridge-library-arduino:test_hal_weak_defaults
)

if [ "$COVERAGE_MODE" = false ]; then
    echo "[host-cpp] Running Pigweed pw_unit_test suites with Bazel..."
    bazel test --config=arduino_host "${TEST_TARGETS[@]}" --test_output=errors --keep_going
    echo "[host-cpp] ALL PIGWEED HOST TESTS PASSED"
    exit 0
fi

OUTPUT_ROOT="${ROOT_DIR}/coverage/arduino"
MIN_LINE="${ARDUINO_COVERAGE_MIN_LINE:-95}"
MIN_BRANCH="${ARDUINO_COVERAGE_MIN_BRANCH:-95}"
MIN_FUNCTION="${ARDUINO_COVERAGE_MIN_FUNCTION:-95}"
mkdir -p "${OUTPUT_ROOT}"

echo "[host-cpp] Running Pigweed pw_unit_test suites with Bazel coverage..."
bazel coverage --config=arduino_host "${TEST_TARGETS[@]}" \
    --combined_report=lcov \
    --instrumentation_filter='//mcubridge-library-arduino' \
    --test_output=errors \
    --keep_going

REPORT="$(find -L bazel-out -type f -name '_coverage_report.dat' -print -quit 2>/dev/null || true)"
if [ -z "${REPORT}" ] || [ ! -s "${REPORT}" ]; then
    echo "[host-cpp] ERROR: Bazel did not produce an LCOV coverage report." >&2
    exit 1
fi
install -m 0644 "${REPORT}" "${OUTPUT_ROOT}/coverage.info"

python3 - "${OUTPUT_ROOT}/coverage.info" "${OUTPUT_ROOT}/summary.txt" \
    "${MIN_LINE}" "${MIN_BRANCH}" "${MIN_FUNCTION}" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

report, summary, min_line, min_branch, min_function = sys.argv[1:]
minima = {
    "line": float(min_line),
    "branch": float(min_branch),
    "function": float(min_function),
}
totals = {
    "line": [0, 0],
    "branch": [0, 0],
    "function": [0, 0],
}
# Match main's gcovr --exclude-branches-by-pattern rule for fatal assert macros.
FATAL_ASSERT_RE = re.compile(r"\bPW_(?:D?ASSERT|D?CHECK[A-Z_]*)\s*\(")
source = ""
source_lines: list[str] = []
functions: dict[str, int] = {}
declared_functions: set[str] = set()
per_file_rows: list[str] = []

def include(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "mcubridge-library-arduino/src/" in normalized and not (
        normalized.endswith(".h")
        or ".pb.c" in normalized
        or "/protocol/" in normalized
    )

def finish_file() -> None:
    if not source or not include(source):
        return
    fn_total = len(declared_functions)
    fn_hit = sum(functions.get(name, 0) > 0 for name in declared_functions)
    totals["line"][0] += line_total
    totals["line"][1] += line_hit
    totals["branch"][0] += branch_total
    totals["branch"][1] += branch_hit
    totals["function"][0] += fn_total
    totals["function"][1] += fn_hit
    lp = 100.0 if line_total == 0 else 100.0 * line_hit / line_total
    bp = 100.0 if branch_total == 0 else 100.0 * branch_hit / branch_total
    fp = 100.0 if fn_total == 0 else 100.0 * fn_hit / fn_total
    per_file_rows.append(
        f"  {source}: line {lp:5.1f}% ({line_hit}/{line_total}), "
        f"branch {bp:5.1f}% ({branch_hit}/{branch_total}), "
        f"func {fp:5.1f}% ({fn_hit}/{fn_total})"
    )

line_total = line_hit = branch_total = branch_hit = 0
for raw in Path(report).read_text(encoding="utf-8").splitlines():
    if raw.startswith("SF:"):
        finish_file()
        source = raw[3:]
        src_path = Path(source)
        source_lines = src_path.read_text(encoding="utf-8").splitlines() if src_path.is_file() else []
        functions = {}
        declared_functions = set()
        line_total = line_hit = branch_total = branch_hit = 0
    elif raw.startswith("FN:"):
        parts = raw[3:].split(",", 1)
        if len(parts) == 2:
            declared_functions.add(parts[1])
    elif raw.startswith("FNDA:"):
        parts = raw[5:].split(",", 1)
        if len(parts) == 2:
            functions[parts[1]] = int(parts[0])
    elif raw.startswith("DA:"):
        parts = raw[3:].split(",")
        if len(parts) >= 2:
            line_total += 1
            line_hit += int(parts[1]) > 0
    elif raw.startswith("BRDA:"):
        parts = raw[5:].split(",")
        if len(parts) == 4 and parts[3] != "-":
            lineno = int(parts[0])
            if 0 < lineno <= len(source_lines) and FATAL_ASSERT_RE.search(source_lines[lineno - 1]):
                continue
            branch_total += 1
            branch_hit += int(parts[3]) > 0
finish_file()

lines = []
failed = False
for key in ("line", "branch", "function"):
    hit, total = totals[key][1], totals[key][0]
    percentage = 100.0 if total == 0 else 100.0 * hit / total
    lines.append(f"{key.title()} coverage: {percentage:.2f}% ({hit}/{total}), required >= {minima[key]:.2f}%")
    failed |= percentage < minima[key]
text = "\n".join(lines) + "\n"
Path(summary).write_text(text, encoding="utf-8")
if per_file_rows:
    print("\n".join(per_file_rows))
print(text, end="")
if failed:
    raise SystemExit(1)
PY
