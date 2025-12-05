#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_ROTATE_HELPER="${ROOT_DIR}/openwrt-yun-core/scripts/yunbridge-rotate-credentials"

show_help() {
  cat <<'EOF'
Usage: ./tools/rotate_credentials.sh --host <yun-ip> [options]

Options:
  --host HOSTNAME        Target YunBridge host (IP or DNS).
  --user USER            SSH username (default: root).
  --ssh "ARGS"           Extra options passed to ssh.
  --local PATH           Rotate credentials inside PATH (UCI config directory) without SSH.
  --emit-sketch-snippet FILE
                         Write the BRIDGE_SERIAL_SHARED_SECRET snippet to FILE so you can
                         include it from your sketches. (Optional)
  -h, --help             Show this help.
EOF
}

HOST=""
USER="root"
SSH_EXTRA=()
LOCAL_UCI_DIR=""
SNIPPET_PATH=""

extract_serial_secret() {
  awk -F'=' '/^SERIAL_SECRET=/{print $2; exit}'
}

write_sketch_snippet_file() {
  local secret="$1" path="$2"
  if [[ -z "$path" ]]; then
    return
  fi
  if [[ -z "$secret" ]]; then
    echo "[WARN] Missing serial secret; cannot update $path" >&2
    return
  fi
  mkdir -p "$(dirname "$path")"
  cat >"$path" <<EOF
#pragma once
// Include this header before <Bridge.h> in your sketch sources.
#define BRIDGE_SERIAL_SHARED_SECRET "$secret"
#define BRIDGE_SERIAL_SHARED_SECRET_LEN (sizeof(BRIDGE_SERIAL_SHARED_SECRET) - 1)
EOF
  echo "[INFO] Wrote sketch snippet to $path"
}

print_sketch_snippet() {
  local secret="$1"
  if [[ -z "$secret" ]]; then
    return
  fi
  cat <<EOF

// Paste the next lines before including <Bridge.h> in your sketch:
#define BRIDGE_SERIAL_SHARED_SECRET "$secret"
#define BRIDGE_SERIAL_SHARED_SECRET_LEN (sizeof(BRIDGE_SERIAL_SHARED_SECRET) - 1)

EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"; shift 2;;
    --user)
      USER="$2"; shift 2;;
    --ssh)
      SSH_EXTRA+=($2); shift 2;;
    --local)
      LOCAL_UCI_DIR="$2"; shift 2;;
    --emit-sketch-snippet)
      SNIPPET_PATH="$2"; shift 2;;
    --emit-arduino-header)
      echo "[WARN] --emit-arduino-header is deprecated; use --emit-sketch-snippet instead." >&2
      SNIPPET_PATH="$2"; shift 2;;
    --sync-arduino)
      echo "[ERROR] --sync-arduino is no longer supported; paste the snippet into your sketch instead." >&2
      exit 1;;
    -h|--help)
      show_help; exit 0;;
    *)
      echo "Unknown option: $1" >&2; show_help; exit 1;;
  esac
done

if [[ -n "$LOCAL_UCI_DIR" ]]; then
  if [[ ! -x "$LOCAL_ROTATE_HELPER" ]]; then
    echo "Local helper $LOCAL_ROTATE_HELPER not found; run inside the repository." >&2
    exit 1
  fi
  if [[ ! -d "$LOCAL_UCI_DIR" ]]; then
    echo "--local expects a directory containing your UCI configs (e.g. <root>/etc/config)." >&2
    exit 1
  fi
  if ! command -v uci >/dev/null 2>&1; then
    echo "The 'uci' CLI is required to run the local helper. Install it or run inside an OpenWrt rootfs." >&2
    exit 1
  fi
  if ! OUTPUT=$(sudo env UCI_CONFIG_DIR="$LOCAL_UCI_DIR" "$LOCAL_ROTATE_HELPER"); then
    exit 1
  fi
  if [[ -n "$OUTPUT" ]]; then
    printf '%s\n' "$OUTPUT"
  fi
  SECRET=$(printf '%s\n' "$OUTPUT" | extract_serial_secret)
  if [[ -n "$SNIPPET_PATH" ]]; then
    write_sketch_snippet_file "$SECRET" "$SNIPPET_PATH"
  fi
  print_sketch_snippet "$SECRET"
  exit 0
fi

if [[ -z "$HOST" ]]; then
  echo "--host is required (or use --local)." >&2
  exit 1
fi

SSH_CMD=(ssh "${SSH_EXTRA[@]}" "$USER@$HOST" -- /usr/bin/yunbridge-rotate-credentials)
if ! OUTPUT=$("${SSH_CMD[@]}"); then
  exit 1
fi

if [[ -n "$OUTPUT" ]]; then
  printf '%s\n' "$OUTPUT"
fi

SECRET=$(printf '%s\n' "$OUTPUT" | extract_serial_secret)
if [[ -n "$SNIPPET_PATH" ]]; then
  write_sketch_snippet_file "$SECRET" "$SNIPPET_PATH"
fi
print_sketch_snippet "$SECRET"
