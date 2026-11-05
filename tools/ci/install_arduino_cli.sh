#!/usr/bin/env bash
set -euo pipefail

# Pin the CLI and verify the official release artifact instead of scraping the
# GitHub "latest" page (which can fail on hosted runners due to rate limiting).
readonly VERSION="1.5.1"
readonly SHA256="28a8e119c498a25607821c36cb2dc49e8463941b261a0d99091baa7bc692dd2b"
readonly URL="https://downloads.arduino.cc/arduino-cli/arduino-cli_${VERSION}_Linux_64bit.tar.gz"

DEST="${BINDIR:-$PWD/bin}"
mkdir -p "$DEST"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
ARCHIVE="$TMP_DIR/arduino-cli.tar.gz"

curl --fail --show-error --location \
  --retry 5 --retry-all-errors --retry-delay 2 \
  --connect-timeout 30 --max-time 300 \
  "$URL" --output "$ARCHIVE"

printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum --check --status
tar -xzf "$ARCHIVE" -C "$DEST" arduino-cli
chmod 0755 "$DEST/arduino-cli"
"$DEST/arduino-cli" version
