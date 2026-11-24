#!/usr/bin/env bash
set -euo pipefail

# Install Pigweed's pinned Linux amd64 Bloaty binary from Google CIPD
# (matches third_party/pigweed/pw_env_setup/py/pw_env_setup/cipd_setup/pigweed.json).
readonly BLOATY_TAG="version:2@4a601b636e2347322d0371c8bf8ca5eaeaca4bac"
readonly SHA256="86bb02aeeaf27afa47da8cf3de0452de124067f944377a19add7c92742ad7d40"
readonly URL="https://chrome-infra-packages.appspot.com/dl/fuchsia/third_party/3pp/bloaty/linux-amd64/+/${BLOATY_TAG}"

if command -v bloaty >/dev/null 2>&1 && bloaty --version >/dev/null 2>&1; then
  bloaty --version
  exit 0
fi

DEST="${BINDIR:-$PWD/bin}"
mkdir -p "$DEST"

if [ -x "$DEST/bloaty" ] && "$DEST/bloaty" --version >/dev/null 2>&1; then
  "$DEST/bloaty" --version
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
ARCHIVE="$TMP_DIR/bloaty.zip"

curl --fail --show-error --location \
  --retry 5 --retry-all-errors --retry-delay 2 \
  --connect-timeout 30 --max-time 300 \
  "$URL" --output "$ARCHIVE"

printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum --check --status
unzip -q -j "$ARCHIVE" "bin/bloaty" -d "$DEST"
chmod 0755 "$DEST/bloaty"
"$DEST/bloaty" --version
