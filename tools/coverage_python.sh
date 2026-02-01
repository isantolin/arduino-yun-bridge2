#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_COVERAGE_ROOT="$ROOT_DIR/coverage/python"
DEFAULT_TARGET="openwrt-mcu-bridge/tests"

usage() {
  cat <<'EOF'
Usage: tools/coverage_python.sh [--output-root DIR] [--no-html] [--json] [--] [pytest args...]

Options:
  --output-root DIR  Output directory (default: coverage/python)
  --no-html           Disable HTML coverage report
  --json              Emit coverage.json (coverage.py JSON)
  -h, --help          Show this help

Any remaining arguments are passed to pytest. If no pytest args are supplied,
the default target is openwrt-mcu-bridge/tests.
EOF
}

COVERAGE_ROOT="$DEFAULT_COVERAGE_ROOT"
ENABLE_HTML=1
ENABLE_JSON=0

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

if ! command -v pytest >/dev/null 2>&1; then
  # Avoid relying on a globally-installed `pytest` entrypoint.
  if ! python -c "import pytest" >/dev/null 2>&1; then
    echo "[coverage_python] pytest no está instalado en el entorno actual." >&2
    exit 1
  fi
fi

mkdir -p "$COVERAGE_ROOT"
export COVERAGE_FILE="$COVERAGE_ROOT/.coverage"

if ! python -c "import pytest_cov" >/dev/null 2>&1; then
  echo "[coverage_python] Instala pytest-cov (pip install pytest-cov) antes de ejecutar este script." >&2
  exit 1
fi

PYTEST_ARGS=()
if [[ $# -gt 0 ]]; then
  PYTEST_ARGS=("$@")
else
  PYTEST_ARGS=("$DEFAULT_TARGET")
fi

python -m pytest \
  -q \
  -o log_cli=false \
  --disable-warnings \
  --timeout=30 \
  --timeout-method=thread \
  --cov=mcubridge \
  --cov-branch \
  --cov-report=xml:"$COVERAGE_ROOT/coverage.xml" \
  $( [[ "$ENABLE_HTML" -eq 1 ]] && echo "--cov-report=html:$COVERAGE_ROOT/html" ) \
  --cov-report=term \
  "${PYTEST_ARGS[@]}"

if [[ "$ENABLE_JSON" -eq 1 ]]; then
  python -m coverage json \
    --include "$ROOT_DIR/openwrt-mcu-bridge/mcubridge/*" \
    -o "$COVERAGE_ROOT/coverage.json" >/dev/null
fi
