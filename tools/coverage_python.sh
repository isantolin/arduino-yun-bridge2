#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT_DIR/typings:$ROOT_DIR/typings/stubs:$ROOT_DIR:$ROOT_DIR/mcubridge:$ROOT_DIR/mcubridge-client-examples:$ROOT_DIR/mcubridge-gateway:${PYTHONPATH:-}"
DEFAULT_COVERAGE_ROOT="$ROOT_DIR/coverage/python"
DEFAULT_TARGETS=("mcubridge/tests" "mcubridge-client-examples/client_tests" "mcubridge-gateway/tests")

usage() {
  cat <<'EOF'
Usage: tools/coverage_python.sh [--output-root DIR] [--no-html] [--json] [--] [pytest args...]

Options:
  --output-root DIR  Output directory (default: coverage/python)
  --no-html           Disable HTML coverage report
  --json              Emit coverage.json (coverage.py JSON)
  -h, --help          Show this help

Any remaining arguments are passed to pytest. If no pytest args are supplied,
the default targets are mcubridge/tests and mcubridge-client-examples/client_tests.

Environment:
PYTHON_COVERAGE_MIN  Minimum total coverage percentage (default: 95)
EOF
}

COVERAGE_ROOT="$DEFAULT_COVERAGE_ROOT"
ENABLE_HTML=1
ENABLE_JSON=0
PYTHON_COVERAGE_MIN=${PYTHON_COVERAGE_MIN:-95}

PYTHON_BIN="${PYTHON_EXE:-python}"
echo "[coverage_python] Debug: Python path: $(which $PYTHON_BIN || echo 'not found') ($PYTHON_BIN)"
echo "[coverage_python] Debug: Python version: $($PYTHON_BIN --version)"
$PYTHON_BIN -m pip list | grep pytest

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

$PYTHON_BIN -m coverage run --rcfile="$ROOT_DIR/pyproject.toml" -m pytest \
  -q \
  -p pytest_asyncio \
  -o log_cli=false \
  --timeout=300 \
  --timeout-method=thread \
  "${PYTEST_ARGS[@]}"

if command -v socat >/dev/null 2>&1 && [[ -x "$ROOT_DIR/tools/compile_emulator.sh" ]]; then
  echo "[coverage_python] Running hardware emulation suite for complete integration coverage..."
  "$ROOT_DIR/tools/compile_emulator.sh" >/dev/null 2>&1 || true

  GW_PID=""
  if $PYTHON_BIN -c "import grpclib" >/dev/null 2>&1; then
    $PYTHON_BIN mcubridge-gateway/gateway.py --no-tls --port 8443 >/dev/null 2>&1 &
    GW_PID=$!
    sleep 2
  fi

  $PYTHON_BIN -m coverage run --append --rcfile="$ROOT_DIR/pyproject.toml" tools/emulation_runner.py \
    --firmware mcubridge-library-arduino/tests/bridge_emulator \
    mcubridge-client-examples/process_test.py \
    mcubridge-client-examples/mailbox_read_test.py \
    mcubridge-client-examples/sensor_reader_test.py \
    mcubridge-client-examples/all_features_test.py \
    mcubridge-client-examples/console_test.py \
    mcubridge-client-examples/datastore_test.py \
    mcubridge-client-examples/fileio_test.py \
    mcubridge-client-examples/led13_test.py \
    mcubridge-client-examples/bootloader_test.py \
    mcubridge-client-examples/spi_test.py || true

  if [[ -n "$GW_PID" ]]; then
    kill "$GW_PID" 2>/dev/null || true
  fi
fi

$PYTHON_BIN -m coverage combine --rcfile="$ROOT_DIR/pyproject.toml" || true
$PYTHON_BIN -m coverage xml -o "$COVERAGE_ROOT/coverage.xml"
if [[ "$ENABLE_HTML" -eq 1 ]]; then
  $PYTHON_BIN -m coverage html -d "$COVERAGE_ROOT/html"
fi

if [[ "$ENABLE_JSON" -eq 1 ]]; then
  $PYTHON_BIN -m coverage json \
    --include "$ROOT_DIR/mcubridge/mcubridge/*" \
    -o "$COVERAGE_ROOT/coverage.json" >/dev/null
fi

$PYTHON_BIN -m coverage report --data-file="$COVERAGE_ROOT/.coverage" --fail-under="${PYTHON_COVERAGE_MIN}"

