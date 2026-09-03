#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="$ROOT_DIR/typings:$ROOT_DIR/typings/stubs:$ROOT_DIR:$ROOT_DIR/mcubridge:$ROOT_DIR/mcubridge-client-examples:$ROOT_DIR/mcubridge-gateway:${PYTHONPATH:-}"
DEFAULT_COVERAGE_ROOT="$ROOT_DIR/coverage/python"
DEFAULT_TARGETS=("mcubridge/tests" "mcubridge-client-examples/tests" "mcubridge-gateway/tests")

usage() {
  cat <<'EOF'
Usage: tools/ci/coverage_python.sh [--output-root DIR] [--no-html] [--json] [--] [pytest args...]

Options:
  --output-root DIR  Output directory (default: coverage/python)
  --no-html           Disable HTML coverage report
  --json              Emit coverage.json (coverage.py JSON)
  -h, --help          Show this help

Any remaining arguments are passed to pytest. If no pytest args are supplied,
the default targets are mcubridge/tests and mcubridge-client-examples/tests.

Environment:
PYTHON_COVERAGE_MIN         Minimum total coverage percentage (default: 95)
PYTHON_COVERAGE_MIN_BRANCH  Minimum pure branch coverage percentage (default: 95)
EOF
}

COVERAGE_ROOT="$DEFAULT_COVERAGE_ROOT"
ENABLE_HTML=1
ENABLE_JSON=0
PYTHON_COVERAGE_MIN=${PYTHON_COVERAGE_MIN:-95}
PYTHON_COVERAGE_MIN_BRANCH=${PYTHON_COVERAGE_MIN_BRANCH:-95}

if [ -n "${PYTHON_EXE:-}" ]; then
  PYTHON_BIN="${PYTHON_EXE}"
elif [ -n "${VIRTUAL_ENV:-}" ]; then
  PYTHON_BIN=$(command -v python || command -v python3)
elif [ -x "${ROOT_DIR}/.tox/coverage/bin/python" ]; then
  PYTHON_BIN="${ROOT_DIR}/.tox/coverage/bin/python"
elif [ -x "${ROOT_DIR}/.tox/py313/bin/python" ]; then
  PYTHON_BIN="${ROOT_DIR}/.tox/py313/bin/python"
else
  PYTHON_BIN=$(command -v python3 || command -v python)
fi
echo "[coverage_python] Debug: Python path: $(which $PYTHON_BIN 2>/dev/null || echo "$PYTHON_BIN") ($PYTHON_BIN)"
echo "[coverage_python] Debug: Python version: $($PYTHON_BIN --version)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-root)
      COVERAGE_ROOT="$2"
      shift 2
      ;;
    --no-html)
      ENABLE_HTML=0
      shift
      ;;
    --json)
      ENABLE_JSON=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

if ! $PYTHON_BIN -m pytest --version >/dev/null 2>&1; then
  # Avoid relying on a globally-installed `pytest` entrypoint.
  if ! $PYTHON_BIN -c "import pytest" >/dev/null 2>&1; then
    echo "[coverage_python] pytest no está instalado en el entorno actual." >&2
    exit 1
  fi
fi

mkdir -p "$COVERAGE_ROOT"
export COVERAGE_FILE="$COVERAGE_ROOT/.coverage"

if ! $PYTHON_BIN -c "import pytest_cov" >/dev/null 2>&1; then
  echo "[coverage_python] Instala pytest-cov (pip install pytest-cov) antes de ejecutar este script." >&2
  exit 1
fi

PYTEST_ARGS=()
if [[ $# -gt 0 ]]; then
  PYTEST_ARGS=("$@")
else
  PYTEST_ARGS=("${DEFAULT_TARGETS[@]}")
fi

$PYTHON_BIN -m pytest \
  -vv \
  -p pytest_asyncio \
  --timeout=300 \
  --timeout-method=thread \
  --cov="$ROOT_DIR/mcubridge/mcubridge" \
  --cov="$ROOT_DIR/mcubridge-client-examples/mcubridge_client" \
  --cov="$ROOT_DIR/mcubridge-gateway" \
  --cov-branch \
  --cov-fail-under="${PYTHON_COVERAGE_MIN}" \
  --cov-report=xml:"$COVERAGE_ROOT/coverage.xml" \
  $( [[ "$ENABLE_HTML" -eq 1 ]] && echo "--cov-report=html:$COVERAGE_ROOT/html" ) \
  --cov-report=term-missing \
  "${PYTEST_ARGS[@]}"

$PYTHON_BIN -m coverage json \
  --data-file="$COVERAGE_FILE" \
  -o "$COVERAGE_ROOT/coverage.json" >/dev/null

BRANCH_CHECK_OUTPUT=$($PYTHON_BIN -c '
import json, sys
with open("'"$COVERAGE_ROOT"'/coverage.json") as f:
    totals = json.load(f).get("totals", {})
branch_pct = totals.get("percent_branches_covered", 0.0)
num_branches = totals.get("num_branches", 0)
covered = totals.get("covered_branches", 0)
min_branch = float("'"$PYTHON_COVERAGE_MIN_BRANCH"'")
print(f"{branch_pct:.2f}% ({covered}/{num_branches})")
if branch_pct < min_branch:
    sys.exit(1)
') || {
  echo "FAIL Required pure branch test coverage of ${PYTHON_COVERAGE_MIN_BRANCH}% not reached. Pure branch coverage: ${BRANCH_CHECK_OUTPUT}" >&2
  exit 1
}

echo "Required pure branch test coverage of ${PYTHON_COVERAGE_MIN_BRANCH}% reached. Pure branch coverage: ${BRANCH_CHECK_OUTPUT}"

