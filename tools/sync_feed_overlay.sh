#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: tools/sync_feed_overlay.sh [OPTIONS]

Populate feeds/yunbridge/ with the canonical package sources from the
repository root so the OpenWrt SDK can consume them as a local feed.

Options:
  --dest PATH   Destination feed directory (default: repo_root/feeds/yunbridge)
  --clean       Remove existing package subdirectories before copying.
  -h, --help    Show this message and exit.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_DIR="$REPO_ROOT/feeds/yunbridge"
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

mkdir -p "$DEST_DIR"

if [[ $CLEAN -eq 1 ]]; then
    echo "[sync-feed] Cleaning $DEST_DIR"
    find "$DEST_DIR" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
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
    mkdir -p "$dest"
    cp -a "$src/." "$dest/"
done
