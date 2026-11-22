#!/usr/bin/env bash
# Retry Ubuntu APT update (and optional package install) using independent
# official Ubuntu mirrors when the runner's configured mirrors suffer transient
# DNS, HTTP 5xx, timeout, or fetch failures. Intended for ephemeral
# GitHub-hosted Ubuntu runners; preserves deb822 Signed-By GPG verification.
#
# Usage:
#   bash tools/ci/apt_update_with_fallback.sh
#   bash tools/ci/apt_update_with_fallback.sh [--no-install-recommends] pkg1 [pkg2 ...]
set -Eeuo pipefail

APT_OPTIONS=(
  -o Acquire::Retries=5
  -o Acquire::http::Timeout=25
  -o Acquire::https::Timeout=25
)
FALLBACK_MIRRORS=(
  "https://mirrors.edge.kernel.org/ubuntu"
  "https://ubuntu.c3sl.ufpr.br/ubuntu"
  "https://mirror.units.it/ubuntu/ubuntu"
)

APT_UPDATE_TIMEOUT="${APT_UPDATE_TIMEOUT:-90}"
APT_DOWNLOAD_TIMEOUT="${APT_DOWNLOAD_TIMEOUT:-240}"
SOURCES_FILE="${APT_SOURCES_FILE:-/etc/apt/sources.list.d/ubuntu.sources}"
APT_RETRIES_CONF="${APT_RETRIES_CONF:-/etc/apt/apt.conf.d/99-mcubridge-ci-retries}"
APT_SUDO="${APT_SUDO-sudo}"

run_priv() {
  if [[ -n "$APT_SUDO" ]]; then
    "$APT_SUDO" "$@"
  else
    "$@"
  fi
}

INSTALL_ARGS=("$@")
PACKAGES=()
for arg in "${INSTALL_ARGS[@]}"; do
  if [[ "$arg" != -* ]]; then
    PACKAGES+=("$arg")
  fi
done

all_packages_installed() {
  if (( ${#PACKAGES[@]} == 0 )); then
    return 1
  fi
  if ! command -v dpkg-query >/dev/null 2>&1; then
    return 1
  fi
  local pkg status
  for pkg in "${PACKAGES[@]}"; do
    status="$(dpkg-query -W -f='${Status}\n' "$pkg" 2>/dev/null || true)"
    if [[ "$status" != "install ok installed" ]]; then
      return 1
    fi
  done
  return 0
}

configure_apt_retries() {
  local conf_dir
  conf_dir="$(dirname "$APT_RETRIES_CONF")"
  if run_priv test -d "$conf_dir"; then
    cat <<'EOF' | run_priv tee "$APT_RETRIES_CONF" >/dev/null
Acquire::Retries "5";
Acquire::http::Timeout "25";
Acquire::https::Timeout "25";
EOF
  fi
}

# Never fall back across mirrors on cryptographic verification, hash integrity,
# or nonexistent package errors.
has_hard_apt_error() {
  local log_file="$1"
  grep -Eqi \
    'NO_PUBKEY|EXPKEYSIG|BADSIG|GPG error|is not signed|Hash Sum mismatch|Clearsigned file isn'\''t valid|The following signatures couldn'\''t be verified|Release file for .* is expired|Unable to locate package|has no installation candidate' \
    "$log_file"
}

# Detect transient network / HTTP 5xx / timeout / DNS failures affecting Ubuntu
# or fallback mirrors (single-line or two-line Err: + reason output).
is_recoverable_ubuntu_mirror_failure() {
  local log_file="$1"
  if has_hard_apt_error "$log_file"; then
    return 1
  fi

  local ubuntu_host_re='(https?://([^/[:space:]]*\.)?(archive|security|ports|old-releases)\.ubuntu\.com|([^/[:space:]]*\.)?ubuntu\.com|ubuntu\.c3sl\.ufpr\.br|mirrors\.edge\.kernel\.org|mirror\.units\.it|mirror\+file:)'
  local transient_err_re='Temporary failure resolving|Could not resolve|Name or service not known|Temporary failure in name resolution|500[[:space:]]+Internal Server Error|502[[:space:]]+Bad Gateway|503[[:space:]]+Service Unavailable|504[[:space:]]+Gateway Time-?out|Could not connect|Connection timed out|Could not wait for server fd|Connection failed|Connection reset by peer|Unable to fetch some archives|Failed to fetch|Some index files failed'

  if grep -Eqi "(Err:|Failed to fetch|Temporary failure resolving|Could not resolve|Could not connect|Cannot initiate the connection).*${ubuntu_host_re}|${ubuntu_host_re}.*(Temporary failure resolving|Could not resolve|Name or service not known|Temporary failure in name resolution|50[0234]|Connection timed out|Could not connect|Connection failed)" "$log_file" &&
      grep -Eqi "$transient_err_re" "$log_file"; then
    return 0
  fi
  return 1
}

apt_update() {
  local attempt apt_log apt_status delay
  for attempt in 1 2 3; do
    apt_log="$(mktemp)"
    apt_status=0
    run_priv timeout --kill-after=15 "$APT_UPDATE_TIMEOUT" \
      apt-get "${APT_OPTIONS[@]}" update 2>&1 | tee "$apt_log" || apt_status=$?

    if (( apt_status == 124 || apt_status == 137 )); then
      echo "::warning::APT update exceeded ${APT_UPDATE_TIMEOUT}s timeout; switching to a fallback mirror."
      rm -f "$apt_log"
      return 10
    fi

    # Microsoft package feeds can rate-limit shared GitHub runners even
    # when every Ubuntu index was downloaded successfully. If APT itself
    # succeeded and the only 429 is from packages.microsoft.com, keep the
    # usable Ubuntu indexes and do not fail unrelated build jobs.
    if grep -Eqi '(^|[[:space:]])429([[:space:]]|$)|Too Many Requests' "$apt_log"; then
      if (( apt_status == 0 )) &&
          grep -Fq 'packages.microsoft.com' "$apt_log" &&
          ! grep -Eqi '(Err:|Failed to fetch).*(([^/[:space:]]*\.)?ubuntu\.com|ubuntu\.c3sl\.ufpr\.br|mirrors\.edge\.kernel\.org|mirror\.units\.it|mirror\+file:)' "$apt_log"; then
        echo "::warning::Ignoring Microsoft package-feed HTTP 429; Ubuntu package indexes updated successfully."
        rm -f "$apt_log"
        return 0
      fi
      rm -f "$apt_log"
      if (( attempt < 3 )); then
        delay=$((attempt * 15))
        echo "::warning::APT repository rate-limited this runner (HTTP 429); retrying in ${delay}s (attempt $((attempt + 1))/3)."
        sleep "$delay"
        continue
      fi
      echo "::error::APT repository continued returning HTTP 429 after three attempts."
      return 1
    fi

    if is_recoverable_ubuntu_mirror_failure "$apt_log"; then
      rm -f "$apt_log"
      return 10
    fi
    if has_hard_apt_error "$apt_log" || grep -Eqi 'Failed to fetch|Some index files failed' "$apt_log"; then
      rm -f "$apt_log"
      return 1
    fi
    rm -f "$apt_log"
    if (( apt_status != 0 )); then
      return "$apt_status"
    fi
    return 0
  done
  return 1
}

apt_install() {
  local apt_log apt_status
  apt_log="$(mktemp)"
  apt_status=0
  run_priv timeout --kill-after=15 "$APT_DOWNLOAD_TIMEOUT" \
    env DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTIONS[@]}" install --download-only -y "$@" \
    2>&1 | tee "$apt_log" || apt_status=$?

  if (( apt_status == 124 || apt_status == 137 )); then
    echo "::warning::APT package download exceeded ${APT_DOWNLOAD_TIMEOUT}s timeout; switching to a fallback mirror while keeping cached archives."
    rm -f "$apt_log"
    return 10
  fi
  if (( apt_status != 0 )); then
    if is_recoverable_ubuntu_mirror_failure "$apt_log"; then
      rm -f "$apt_log"
      return 10
    fi
    rm -f "$apt_log"
    return "$apt_status"
  fi
  rm -f "$apt_log"

  # Every required .deb archive is verified and cached locally; unpack and
  # configure offline without a network timeout so dpkg is never interrupted.
  run_priv env DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTIONS[@]}" install --no-download -y "$@"
}

apt_run_cycle() {
  local status=0
  apt_update || status=$?
  if (( status != 0 )); then
    return "$status"
  fi
  if (( ${#INSTALL_ARGS[@]} > 0 )); then
    apt_install "${INSTALL_ARGS[@]}" || status=$?
    return "$status"
  fi
  return 0
}

if all_packages_installed; then
  echo "All requested APT packages are already installed: ${PACKAGES[*]}"
  exit 0
fi

configure_apt_retries

echo "::group::APT operation using the runner's configured Ubuntu mirrors"
apt_status=0
if apt_run_cycle; then
  echo "APT operation succeeded using the runner's configured Ubuntu mirrors."
  echo "::endgroup::"
  exit 0
else
  apt_status=$?
fi
echo "::endgroup::"
if (( apt_status != 10 )); then
  echo "::error::APT failed for a non-recoverable reason; fallback mirrors were not changed."
  exit "$apt_status"
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "::error::curl is unavailable; cannot validate Ubuntu fallback mirrors."
  exit 1
fi

if ! run_priv test -f "$SOURCES_FILE"; then
  echo "::error::Ubuntu deb822 sources file not found at $SOURCES_FILE; refusing to rewrite unknown APT configuration."
  exit 1
fi

mapfile -t suites < <(
  run_priv awk 'tolower($1) == "suites:" { for (i = 2; i <= NF; i++) print $i }' "$SOURCES_FILE" |
    sort -u
)

if (( ${#suites[@]} == 0 )); then
  echo "::error::No Ubuntu suites found in $SOURCES_FILE."
  exit 1
fi

BACKUP_FILE="${RUNNER_TEMP:-/tmp}/ubuntu.sources.original.$$"
run_priv cp --preserve=mode,ownership "$SOURCES_FILE" "$BACKUP_FILE"
cleanup_backup() {
  rm -f "$BACKUP_FILE"
}
trap cleanup_backup EXIT

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

  # Ubuntu 24.04+ runner images use deb822 sources. Rewrite both mirror+file
  # and direct archive/security.ubuntu.com URI entries while preserving
  # Suites, Components, and Signed-By archive verification.
  run_priv cp --preserve=mode,ownership "$BACKUP_FILE" "$SOURCES_FILE"
  if ! run_priv sed -E -i \
      -e "s#^URIs:[[:space:]]*mirror\+file:.*#URIs: ${mirror}/#" \
      -e "s#^URIs:[[:space:]]*https?://([^/[:space:]]*\.)?(archive|security)\.ubuntu\.com/ubuntu/?[[:space:]]*\$#URIs: ${mirror}/#" \
      "$SOURCES_FILE" ||
      ! run_priv grep -Fq "URIs: ${mirror}/" "$SOURCES_FILE"; then
    run_priv cp --preserve=mode,ownership "$BACKUP_FILE" "$SOURCES_FILE"
    echo "::warning::Could not apply $mirror; trying the next fallback."
    continue
  fi

  echo "::notice::Ubuntu APT is retrying through $mirror."
  apt_status=0
  if apt_run_cycle; then
    echo "APT operation succeeded through $mirror."
    exit 0
  else
    apt_status=$?
  fi

  run_priv cp --preserve=mode,ownership "$BACKUP_FILE" "$SOURCES_FILE"
  if (( apt_status != 10 )); then
    echo "::error::APT failed through $mirror for a non-recoverable reason; restored original Ubuntu sources."
    exit "$apt_status"
  fi
  echo "::warning::Transient mirror failure through $mirror; trying the next fallback."
done

run_priv cp --preserve=mode,ownership "$BACKUP_FILE" "$SOURCES_FILE"
echo "::error::APT failed with the configured mirrors and all validated fallback mirrors; restored the original Ubuntu sources."
exit 1
