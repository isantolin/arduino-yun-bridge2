#!/usr/bin/env bash
# Retry Ubuntu APT using independent mirrors when the runner's configured
# mirror list is unavailable. Intended for ephemeral GitHub-hosted Ubuntu runners.
set -Eeuo pipefail

APT_OPTIONS=(
  -o Acquire::Retries=1
  -o Acquire::http::Timeout=20
  -o Acquire::https::Timeout=20
)
FALLBACK_MIRRORS=(
  "https://ubuntu.c3sl.ufpr.br/ubuntu"
  "https://mirrors.edge.kernel.org/ubuntu"
)

apt_update() {
  local apt_log
  local apt_status=0
  apt_log="$(mktemp)"
  sudo apt-get "${APT_OPTIONS[@]}" update 2>&1 | tee "$apt_log" || apt_status=$?

  # apt-get update can exit zero while warning about unreachable indexes.
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

if ! command -v curl >/dev/null 2>&1; then
  echo "::error::curl is unavailable; cannot validate Ubuntu fallback mirrors."
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

BACKUP_FILE="${RUNNER_TEMP:-/tmp}/ubuntu.sources.original"
sudo cp --preserve=mode,ownership "$SOURCES_FILE" "$BACKUP_FILE"

for mirror in "${FALLBACK_MIRRORS[@]}"; do
  echo "::group::Validate Ubuntu fallback mirror: $mirror"
  mirror_available=true

  for suite in "${suites[@]}"; do
    echo "Checking $mirror/dists/$suite/InRelease"
    if ! curl --fail --silent --show-error --location \
        --retry 2 --connect-timeout 10 --max-time 30 \
        --output /dev/null "$mirror/dists/$suite/InRelease"; then
      echo "Mirror does not provide a reachable InRelease for suite '$suite'."
      mirror_available=false
      break
    fi
  done

  if [[ "$mirror_available" != true ]]; then
    echo "::endgroup::"
    continue
  fi

  echo "Mirror provides every configured Ubuntu suite."
  echo "::endgroup::"

  # Ubuntu 24.04+ runner images use deb822 sources. Replace only the
  # mirror+file URI entries, preserving suites, components and archive signing.
  if ! sudo sed -i \
      -e "s#^URIs:[[:space:]]*mirror+file:.*#URIs: $mirror/#" \
      "$SOURCES_FILE" ||
      ! sudo grep -Fq "URIs: $mirror/" "$SOURCES_FILE"; then
    sudo cp "$BACKUP_FILE" "$SOURCES_FILE"
    echo "::warning::Could not apply $mirror; trying the next fallback."
    continue
  fi

  echo "::notice::Ubuntu APT is retrying through $mirror."
  if apt_update; then
    echo "APT indexes downloaded successfully from $mirror."
    exit 0
  fi

  sudo cp "$BACKUP_FILE" "$SOURCES_FILE"
  echo "::warning::APT update failed through $mirror; trying the next fallback."
done

sudo cp "$BACKUP_FILE" "$SOURCES_FILE"
echo "::error::APT update failed with the configured mirrors and all validated fallback mirrors; restored the original Ubuntu sources."
exit 1
