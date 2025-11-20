#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COVERAGE_ROOT="${COVERAGE_ROOT:-$ROOT_DIR/coverage/python}"
DEFAULT_TARGET="openwrt-yun-bridge/tests"

if ! command -v pytest >/dev/null 2>&1; then
  echo "[coverage_python] pytest no está instalado en el entorno actual." >&2
  exit 1
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

pytest \
  --cov=openwrt-yun-bridge \
  --cov-report=term-missing \
  --cov-report=xml:"$COVERAGE_ROOT/coverage.xml" \
  --cov-report=html:"$COVERAGE_ROOT/html" \
  "${PYTEST_ARGS[@]}"
