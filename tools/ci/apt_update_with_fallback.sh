#!/usr/bin/env bash
# Retry Ubuntu APT using an independent mirror when the runner's configured
# mirror list is unavailable. Intended for ephemeral GitHub-hosted Ubuntu runners.
set -Eeuo pipefail

APT_OPTIONS=(
  -o Acquire::Retries=1
  -o Acquire::http::Timeout=20
  -o Acquire::https::Timeout=20
)

apt_update() {
  local apt_log
  local apt_status=0
  apt_log="$(mktemp)"
  sudo apt-get "${APT_OPTIONS[@]}" update 2>&1 | tee "$apt_log" || apt_status=$?

  # apt-get update may exit zero while emitting warnings for unreachable
  # repositories. Treat failed Ubuntu index downloads as a failed update.
  if grep -Eqi 'Failed to fetch.*(ubuntu|archive|security)|Some index files failed|Temporary failure resolving.*(ubuntu|archive|security)' "$apt_log"; then
    apt_status=1
  fi

  rm -f "$apt_log"
  return "$apt_status"
}

echo "::group::APT update using the runner's configured Ubuntu mirrors"
if apt_update; then
  echo "The configured Ubuntu mirrors are reachable."
  echo "::endgroup::"
  exit 0
fi
echo "::endgroup::"

SOURCES_FILE="/etc/apt/sources.list.d/ubuntu.sources"
FALLBACK_MIRROR="https://ubuntu.c3sl.ufpr.br/ubuntu"

if ! command -v curl >/dev/null 2>&1; then
  echo "::error::curl is unavailable; cannot validate the Ubuntu fallback mirror."
  exit 1
fi

if ! sudo test -f "$SOURCES_FILE"; then
  echo "::error::Ubuntu deb822 sources file not found at $SOURCES_FILE; refusing to rewrite unknown APT configuration."
  exit 1
fi

mapfile -t suites < <(
  sudo awk 'tolower($1) == "suites:" { for (i = 2; i <= NF; i++) print $i }' "$SOURCES_FILE" |
    sort -u
)

if (("${#suites[@]}" == 0)); then
  echo "::error::No Ubuntu suites found in $SOURCES_FILE."
  exit 1
fi

echo "::group::Validate C3SL Ubuntu mirror"
for suite in "${suites[@]}"; do
  echo "Checking $FALLBACK_MIRROR/dists/$suite/InRelease"
  if ! curl --fail --silent --show-error --location \
      --retry 2 --connect-timeout 10 --max-time 30 \
      --output /dev/null "$FALLBACK_MIRROR/dists/$suite/InRelease"; then
    echo "::error::C3SL does not provide a reachable InRelease for suite '$suite'; keeping the original APT configuration."
    echo "::endgroup::"
    exit 1
  fi
done
echo "C3SL provides every configured Ubuntu suite."
echo "::endgroup::"

BACKUP_FILE="${RUNNER_TEMP:-/tmp}/ubuntu.sources.original"
sudo cp --preserve=mode,ownership "$SOURCES_FILE" "$BACKUP_FILE"

# Ubuntu 24.04+ GitHub runner images use deb822 sources. Replace only the
# mirror+file URI entries, preserving suites, components and archive signing.
if ! sudo sed -i \
    -e "s#^URIs:[[:space:]]*mirror+file:.*#URIs: $FALLBACK_MIRROR/#" \
    "$SOURCES_FILE"; then
  sudo cp "$BACKUP_FILE" "$SOURCES_FILE"
  echo "::error::Could not configure the C3SL fallback mirror."
  exit 1
fi

if ! sudo grep -q "^URIs: $FALLBACK_MIRROR/" "$SOURCES_FILE"; then
  sudo cp "$BACKUP_FILE" "$SOURCES_FILE"
  echo "::error::No mirror+file Ubuntu source was replaced; refusing to continue."
  exit 1
fi

echo "::notice::Ubuntu APT is retrying through the C3SL mirror ($FALLBACK_MIRROR)."
if apt_update; then
  echo "APT indexes downloaded successfully from C3SL."
  exit 0
fi

sudo cp "$BACKUP_FILE" "$SOURCES_FILE"
echo "::error::APT update failed with both the configured mirrors and C3SL; restored the original Ubuntu sources."
exit 1
