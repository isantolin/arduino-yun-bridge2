#!/usr/bin/env bash
set -euo pipefail

show_help() {
  cat <<'EOF'
Usage: ./tools/rotate_credentials.sh --host <yun-ip> [options]

Options:
  --host HOSTNAME        Target YunBridge host (IP or DNS).
  --user USER            SSH username (default: root).
  --cred-file PATH       Remote credentials file path (default: /etc/yunbridge/credentials).
  --ssh "ARGS"           Extra options passed to ssh.
  --local PATH           Rotate credentials on the local filesystem (bypass SSH).
  -h, --help             Show this help.
EOF
}

HOST=""
USER="root"
CRED_FILE="/etc/yunbridge/credentials"
SSH_EXTRA=()
LOCAL_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"; shift 2;;
    --user)
      USER="$2"; shift 2;;
    --cred-file)
      CRED_FILE="$2"; shift 2;;
    --ssh)
      SSH_EXTRA+=($2); shift 2;;
    --local)
      LOCAL_PATH="$2"; shift 2;;
    -h|--help)
      show_help; exit 0;;
    *)
      echo "Unknown option: $1" >&2; show_help; exit 1;;
  esac
done

if [[ -n "$LOCAL_PATH" ]]; then
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  LOCAL_SCRIPT="${ROOT_DIR}/openwrt-yun-core/scripts/yunbridge-rotate-credentials"
  if [[ ! -x "$LOCAL_SCRIPT" ]]; then
    echo "Local helper $LOCAL_SCRIPT not found; run inside the repository." >&2
    exit 1
  fi
  sudo "$LOCAL_SCRIPT" "$LOCAL_PATH"
  exit 0
fi

if [[ -z "$HOST" ]]; then
  echo "--host is required (or use --local)." >&2
  exit 1
fi

SSH_CMD=(ssh "${SSH_EXTRA[@]}" "$USER@$HOST" -- /usr/bin/yunbridge-rotate-credentials "$CRED_FILE")
"${SSH_CMD[@]}"
