#!/usr/bin/env bash
set -euo pipefail

show_help() {
  cat <<'EOF'
Usage: ./tools/hardware_smoke_test.sh --host <mcu-ip> [options]

Options:
  --host HOSTNAME        Target McuBridge host (IP or DNS).
  --user USER            SSH username (default: root).
  --ssh "ARGS"           Extra ssh options (e.g. "-i ~/.ssh/id_rsa").
  --local                Run /usr/bin/mcubridge-hw-smoke locally instead of ssh.
  -h, --help             Show this message and exit.
EOF
}

HOST=""
USER="root"
SSH_EXTRA=()
LOCAL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"; shift 2;;
    --user)
      USER="$2"; shift 2;;
    --ssh)
      SSH_EXTRA+=($2); shift 2;;
    --local)
      LOCAL=1; shift;;
    -h|--help)
      show_help; exit 0;;
    *)
      echo "Unknown option: $1" >&2; show_help; exit 1;;
  esac
done

if [[ $LOCAL -eq 1 ]]; then
  /usr/bin/mcubridge-hw-smoke
  exit 0
fi

if [[ -z "$HOST" ]]; then
  echo "--host is required unless --local is provided." >&2
  exit 1
fi

SSH_CMD=(ssh "${SSH_EXTRA[@]}" "$USER@$HOST" -- /usr/bin/mcubridge-hw-smoke)
"${SSH_CMD[@]}"
