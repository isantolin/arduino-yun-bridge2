#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: tools/sync_feed_overlay.sh [OPTIONS]

Populate the destination feed directory with symlinks to the canonical
package sources from the repository root so the OpenWrt SDK can consume them
as a local feed (without copying sources).

Options:
    --dest PATH   Destination feed directory (default: repo_root/feeds)
    --clean       Remove existing managed package entries before linking.
  -h, --help    Show this message and exit.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_DIR="$REPO_ROOT/feeds"
CLEAN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dest)
            if [[ $# -lt 2 ]]; then
                echo "Missing value for --dest" >&2
                exit 1
            fi
            DEST_DIR="$2"
            shift 2
            ;;
        --clean)
            CLEAN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

PACKAGES=(
    luci-app-yunbridge
    openwrt-yun-bridge
    openwrt-yun-core
)

compute_link_target() {
    local src="$1"
    local dest_dir="$2"

    if command -v realpath >/dev/null 2>&1; then
        # GNU coreutils realpath supports --relative-to; fall back to absolute if unsupported.
        realpath --relative-to="$dest_dir" "$src" 2>/dev/null || printf '%s\n' "$src"
        return 0
    fi

    python3 - <<'PY' "$src" "$dest_dir" 2>/dev/null || printf '%s\n' "$src"
import os
import sys

src = sys.argv[1]
dest_dir = sys.argv[2]
print(os.path.relpath(src, dest_dir))
PY
}

mkdir -p "$DEST_DIR"

if [[ $CLEAN -eq 1 ]]; then
    echo "[sync-feed] Cleaning $DEST_DIR"
    for pkg in "${PACKAGES[@]}"; do
        rm -rf "$DEST_DIR/$pkg"
    done
fi

for pkg in "${PACKAGES[@]}"; do
    src="$REPO_ROOT/$pkg"
    dest="$DEST_DIR/$pkg"

    if [[ ! -d "$src" ]]; then
        echo "[sync-feed] ERROR: source directory $src not found" >&2
        exit 1
    fi

    echo "[sync-feed] Syncing $pkg"
    rm -rf "$dest"

    link_target="$(compute_link_target "$src" "$DEST_DIR")"
    ln -s "$link_target" "$dest"
done
