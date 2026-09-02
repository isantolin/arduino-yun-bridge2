#!/usr/bin/env bash
# Hardware Physical E2E Smoke Test Suite Runner
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXAMPLES_DIR="$REPO_ROOT/mcubridge-client-examples"

show_help() {
  cat <<'EOF'
Usage: ./tools/hardware_smoke_test.sh [options]

Options:
  --local                Run _test.py suite locally on this machine (default).
  --host HOSTNAME        Target McuBridge host (IP or DNS) for remote execution via SSH.
  --user USER            SSH username (default: root).
  --ssh "ARGS"           Extra ssh options (e.g. "-o StrictHostKeyChecking=no").
  --socket-path PATH     UNIX socket path (default: /var/run/mcubridge.sock).
  --test TEST_NAME       Run specific test script (e.g. led13_test.py).
  -h, --help             Show this message and exit.
EOF
}

HOST=""
USER="root"
SSH_EXTRA=()
LOCAL=1
SOCKET_PATH="/var/run/mcubridge.sock"
SPECIFIC_TEST=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      LOCAL=0
      shift 2;;
    --user)
      USER="$2"; shift 2;;
    --ssh)
      SSH_EXTRA+=($2); shift 2;;
    --local)
      LOCAL=1; shift;;
    --socket-path)
      SOCKET_PATH="$2"; shift 2;;
    --test)
      SPECIFIC_TEST="$2"; shift 2;;
    -h|--help)
      show_help; exit 0;;
    *)
      echo "Unknown option: $1" >&2; show_help; exit 1;;
  esac
done

# List of client test scripts to execute
ALL_TESTS=(
  "led13_test.py"
  "datastore_test.py"
  "mailbox_read_test.py"
  "console_test.py"
  "sensor_reader_test.py"
  "fileio_test.py"
  "process_test.py"
  "all_features_test.py"
)

if [[ -n "$SPECIFIC_TEST" ]]; then
  TESTS_TO_RUN=("$SPECIFIC_TEST")
else
  TESTS_TO_RUN=("${ALL_TESTS[@]}")
fi

echo "========================================================"
echo " McuBridge Hardware Physical Test Runner (_test.py suite)"
echo "========================================================"

if [[ $LOCAL -eq 1 ]]; then
  echo "Target: Local machine (Socket: $SOCKET_PATH)"
  echo "Executing ${#TESTS_TO_RUN[@]} test(s)..."
  echo "--------------------------------------------------------"

  PASSED=0
  FAILED=0
  START_TOTAL=$(date +%s)

  for test_file in "${TESTS_TO_RUN[@]}"; do
    test_path="$EXAMPLES_DIR/$test_file"
    if [[ ! -f "$test_path" ]]; then
      test_path="$test_file"
    fi

    if [[ ! -f "$test_path" ]]; then
      echo "[-] [FAIL] Test file not found: $test_file"
      FAILED=$((FAILED + 1))
      continue
    fi

    echo -n "[*] Running $(basename "$test_path")... "
    START_TEST=$(date +%s)
    
    if MCUBRIDGE_NON_INTERACTIVE=1 PYTHONPATH="$EXAMPLES_DIR:$REPO_ROOT/mcubridge" python3 "$test_path" --socket-path "$SOCKET_PATH" >/dev/null 2>&1; then
      END_TEST=$(date +%s)
      DIFF=$((END_TEST - START_TEST))
      echo "✅ [PASS] (${DIFF}s)"
      PASSED=$((PASSED + 1))
    else
      END_TEST=$(date +%s)
      DIFF=$((END_TEST - START_TEST))
      echo "❌ [FAIL] (${DIFF}s)"
      FAILED=$((FAILED + 1))
    fi
  done

  END_TOTAL=$(date +%s)
  TOTAL_DIFF=$((END_TOTAL - START_TOTAL))

  echo "========================================================"
  echo "RESULT: $PASSED passed, $FAILED failed in ${TOTAL_DIFF}s"
  echo "========================================================"

  # [SIL-2] Status Snapshot Post-Run Integrity Gate
  echo "[*] Auditing /tmp/mcubridge_status.json health..."
  if ! python3 "$REPO_ROOT/tools/audit_bridge_status.py" --status-path /tmp/mcubridge_status.json; then
    echo "❌ [FAIL] Status audit detected active anomalies in /tmp/mcubridge_status.json"
    exit 1
  fi

  if [[ $FAILED -gt 0 ]]; then
    exit 1
  fi
  exit 0
fi

# Remote execution via SSH
echo "Target: $USER@$HOST (Socket: $SOCKET_PATH)"
echo "Synchronizing test scripts to remote target..."
ssh "${SSH_EXTRA[@]}" "$USER@$HOST" "mkdir -p /tmp/mcubridge-client-examples"
scp -O "${SSH_EXTRA[@]}" -r "$EXAMPLES_DIR"/* "$USER@$HOST:/tmp/mcubridge-client-examples/"

echo "Executing ${#TESTS_TO_RUN[@]} test(s) on remote hardware..."
echo "--------------------------------------------------------"

PASSED=0
FAILED=0
START_TOTAL=$(date +%s)

for test_file in "${TESTS_TO_RUN[@]}"; do
  test_name=$(basename "$test_file")
  echo -n "[*] Running $test_name on $HOST... "
  START_TEST=$(date +%s)

  REMOTE_CMD="MCUBRIDGE_NON_INTERACTIVE=1 PYTHONPATH=/tmp/mcubridge-client-examples python3 /tmp/mcubridge-client-examples/$test_name --socket-path '$SOCKET_PATH'"
  if ssh "${SSH_EXTRA[@]}" "$USER@$HOST" "$REMOTE_CMD" >/dev/null 2>&1; then
    END_TEST=$(date +%s)
    DIFF=$((END_TEST - START_TEST))
    echo "✅ [PASS] (${DIFF}s)"
    PASSED=$((PASSED + 1))
  else
    END_TEST=$(date +%s)
    DIFF=$((END_TEST - START_TEST))
    echo "❌ [FAIL] (${DIFF}s)"
    FAILED=$((FAILED + 1))
  fi
done

END_TOTAL=$(date +%s)
TOTAL_DIFF=$((END_TOTAL - START_TOTAL))

echo "========================================================"
echo "RESULT: $PASSED passed, $FAILED failed in ${TOTAL_DIFF}s"
echo "========================================================"

# [SIL-2] Remote Status Snapshot Post-Run Integrity Gate
echo "[*] Auditing remote /tmp/mcubridge_status.json health..."
REMOTE_JSON=$(ssh "${SSH_EXTRA[@]}" "$USER@$HOST" "cat /tmp/mcubridge_status.json 2>/dev/null || echo ''")
if ! python3 "$REPO_ROOT/tools/audit_bridge_status.py" --raw-json "$REMOTE_JSON"; then
  echo "❌ [FAIL] Remote status audit detected active anomalies in /tmp/mcubridge_status.json"
  exit 1
fi

if [[ $FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
