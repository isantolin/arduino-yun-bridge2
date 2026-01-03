#!/bin/sh
set -eu

# This file is part of Arduino Yun Ecosystem v2.
# Copyright (C) 2025 Ignacio Santolin and contributors
# Target: OpenWrt 25.12.0 (APK System)

# Always run relative paths from the repository root
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

DEPENDENCY_MANIFEST="$PROJECT_ROOT/requirements/runtime.toml"

if [ ! -f "$DEPENDENCY_MANIFEST" ]; then
    cat >&2 <<EOF
[ERROR] Missing dependency manifest at $DEPENDENCY_MANIFEST.
[HINT] Copy the entire arduino-yun-bridge2 repository (including dependencies/) to the device and run ./3_install.sh from that directory.
EOF
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: este script debe ejecutarse como root." >&2
    exit 1
fi

#  --- Configuration Variables ---
INIT_SCRIPT="/etc/init.d/yunbridge"
REQUIRED_SWAP_KB=1048576
MIN_SWAP_KB=$((REQUIRED_SWAP_KB * 99 / 100))
MIN_DISK_KB=51200 # 50MB free required
TMPDIR=/overlay/upper/tmp

# [FIX] Removed --force-reinstall as it is not supported by OpenWrt's apk
LOCAL_APK_INSTALL_FLAGS="--allow-untrusted --force-overwrite"

SERIAL_SECRET_PLACEHOLDER="changeme123"
BOOTSTRAP_SERIAL_SECRET="755142925659b6f5d3ab00b7b280d72fc1cc17f0dad9f52fff9f65efd8caf8e3"

# Keep shell defaults aligned with yunbridge.const
DEFAULT_SERIAL_RETRY_TIMEOUT="0.75"
DEFAULT_SERIAL_RESPONSE_TIMEOUT="3.0"
DEFAULT_SERIAL_RETRY_ATTEMPTS="3"
DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL="0.0"
DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES="3"

# [FIX] Added python3-*.apk to the top to ensure dependencies are installed BEFORE the bridge
PROJECT_APK_PATTERNS="\
python3-*.apk \
openwrt-yun-core-*.apk \
openwrt-yun-bridge-*.apk \
luci-app-yunbridge-*.apk"

UCI_GENERAL_DIRTY=0

#  --- Helper Functions ---
mkdir -p "$TMPDIR"

stop_daemon() {
    if [ ! -x "$INIT_SCRIPT" ]; then
        echo "[INFO] YunBridge daemon not installed, skipping stop."
        return
    fi

    echo "[INFO] Stopping yunbridge daemon if active..."
    $INIT_SCRIPT stop 2>/dev/null || true
    sleep 1

    pids=$(ps w | grep -E 'python[0-9.]*.*yunbridge' | grep -v grep | awk '{print $1}')

    if [ -n "$pids" ]; then
        echo "[WARN] Daemon still running. Sending SIGTERM..."
        kill $pids 2>/dev/null || true
        sleep 2
        pids2=$(ps w | grep -E 'python[0-9.]*.*yunbridge' | grep -v grep | awk '{print $1}')
        if [ -n "$pids2" ]; then
            echo "[WARN] Process will not die. Sending SIGKILL..."
            kill -9 $pids2 2>/dev/null || true
        fi
    else
        echo "[INFO] No running yunbridge daemon process found."
    fi
}

read_swap_total_kb() {
    awk 'NR>1 {sum+=$3} END {print sum+0}' /proc/swaps 2>/dev/null
}

read_swap_total_with_fallback() {
    total=$(read_swap_total_kb)
    if [ "${total:-0}" -eq 0 ]; then
        # BusyBox swapon may vary
        total=$(swapon -s 2>/dev/null | awk 'NR>1 {sum+=$3} END {print sum+0}')
    fi
    echo "${total:-0}"
}

install_dependency() {
    pkg="$1"
    
    # Check for local bundled APK first
    local local_apk=""
    # [FIX] Strict pattern: require a digit after hyphen to avoid partial matches
    # e.g., prevents 'python3' matching 'python3-aiomqtt'
    for candidate in "bin/${pkg}"-[0-9]*.apk; do
        if [ -f "$candidate" ]; then
            local_apk="$candidate"
            break
        fi
    done

    if [ -n "$local_apk" ]; then
        echo "[INFO] Installing $pkg from bundled APK ($local_apk)..."
        # [FIX] removed --force-reinstall
        if apk add $LOCAL_APK_INSTALL_FLAGS "./$local_apk"; then
            return 0
        fi
        echo "[WARN] Failed to install $pkg from bundled APK; trying configured feeds." >&2
    fi

    # Fallback to feed installation.
    echo "[INFO] Ensuring $pkg is installed/updated from feeds..."
    
    # [FIX] Self-healing logic for broken APK state
    if apk add "$pkg"; then
        return 0
    else
        echo "[WARN] 'apk add $pkg' failed. The package database might be inconsistent."
        echo "[INFO] Attempting 'apk fix' to repair system state..."
        apk fix || true
        
        echo "[INFO] Retrying installation of $pkg..."
        if apk add "$pkg"; then
            echo "[INFO] Installation successful after repair."
            return 0
        fi
    fi

    # [FIX] Last resort: check if it's installed anyway
    if apk info -e "$pkg" >/dev/null 2>&1; then
        echo "[WARN] Installation command failed, but '$pkg' appears to be present. Continuing..."
        return 0
    fi

    echo "[ERROR] Failed to install dependency $pkg from feeds or bin/." >&2
    exit 1
}

check_disk_space() {
    local target="$1"
    local available_kb
    available_kb=$(df -k "$target" | awk 'NR==2 {print $4}')
    
    if [ "$available_kb" -lt "$MIN_DISK_KB" ]; then
        echo "[ERROR] Insufficient disk space on $target. Available: ${available_kb}KB, Required: ${MIN_DISK_KB}KB" >&2
        return 1
    fi
    echo "[INFO] Disk space check passed on $target (${available_kb}KB available)."
    return 0
}

install_manifest_pip_requirements() {
    local manifest_path="$DEPENDENCY_MANIFEST"
    local tmp_requirements

    if ! command -v python3 >/dev/null 2>&1; then
        echo "[ERROR] python3 binary not found." >&2
        exit 1
    fi

    if [ ! -f "$manifest_path" ]; then
        echo "[ERROR] Missing dependency manifest at $manifest_path" >&2
        exit 1
    fi

    tmp_requirements=$(mktemp "${TMPDIR:-/tmp}/yunbridge-pip.XXXXXX")
    python3 - "$manifest_path" "$tmp_requirements" <<'PY'
import sys
from pathlib import Path
import tomllib

manifest = Path(sys.argv[1])
output = Path(sys.argv[2])
if not manifest.exists():
    raise SystemExit(f"Missing manifest: {manifest}")

data = tomllib.loads(manifest.read_text())
specs = sorted(
    {
        entry.get("pip", "").strip()
        for entry in data.get("dependency", [])
        if entry.get("pip") and not entry.get("openwrt")
    }
)
if specs:
    output.write_text("\n".join(specs) + "\n")
else:
    output.write_text("")
PY

    if [ ! -s "$tmp_requirements" ]; then
        echo "[INFO] Manifest declares no pip-only dependencies; skipping PyPI install."
        rm -f "$tmp_requirements"
        return
    fi

    if ! command -v pip3 >/dev/null 2>&1; then
        echo "[INFO] pip3 not found; installing python3-pip..."
        install_dependency python3-pip
    fi

    if ! check_disk_space "/overlay"; then
        echo "[ERROR] Not enough space for pip packages." >&2
        rm -f "$tmp_requirements"
        exit 1
    fi

    echo "[INFO] Installing pinned PyPI dependencies..."
    # --break-system-packages might be needed on newer Python/OpenWrt envs
    # Added || true to prevent install failure from stopping the script if user has custom env
    if ! python3 -m pip install --break-system-packages --no-cache-dir --upgrade --pre -r "$tmp_requirements"; then
        echo "[ERROR] Failed to install pinned PyPI dependencies." >&2
        rm -f "$tmp_requirements"
        exit 1
    fi

    rm -f "$tmp_requirements"
}

# Ensure UCI config file exists before trying to access it
ensure_uci_config() {
    if [ ! -f /etc/config/yunbridge ]; then
        echo "[WARN] /etc/config/yunbridge not found (package not installed?). Creating default..."
        touch /etc/config/yunbridge
        uci set yunbridge.general=settings
        uci set yunbridge.general.enabled='1'
        UCI_GENERAL_DIRTY=1
    fi
}

uci_get_general() {
    local key="$1"
    uci -q get "yunbridge.general.${key}" 2>/dev/null || true
}

uci_set_general() {
    local key="$1" value="$2"
    ensure_uci_config
    uci set "yunbridge.general.${key}=$value"
    UCI_GENERAL_DIRTY=1
}

uci_commit_general() {
    if [ "${UCI_GENERAL_DIRTY:-0}" -ne 0 ]; then
        uci commit yunbridge
        UCI_GENERAL_DIRTY=0
    fi
}

ensure_general_default() {
    local key="$1" default_value="$2"
    local current
    current=$(uci_get_general "$key")
    if [ -z "$current" ]; then
        echo "[INFO] Setting default ${key}=$default_value in UCI." >&2
        uci_set_general "$key" "$default_value"
        current="$default_value"
    fi
    printf '%s\n' "$current"
}

generate_random_hex() {
    local length="$1" value=""
    if command -v python3 >/dev/null 2>&1; then
        value=$(python3 - "$length" <<'PY'
import binascii, os, sys
print(binascii.hexlify(os.urandom(int(sys.argv[1]))).decode(), end="")
PY
        )
    fi
    if [ -z "$value" ]; then
        value=$(head -c "$length" /dev/urandom | hexdump -v -e '/1 "%02x"')
    fi
    printf '%s\n' "$value"
}

generate_random_b64() {
    local length="$1" value=""
    if command -v python3 >/dev/null 2>&1; then
        value=$(python3 - "$length" <<'PY'
import base64, os, sys
print(base64.b64encode(os.urandom(int(sys.argv[1]))).decode().rstrip('='), end="")
PY
        )
    fi
    if [ -z "$value" ]; then
        value=$(generate_random_hex "$length")
    fi
    printf '%s\n' "$value"
}

normalize_tls_path_in_uci() {
    # Deprecated: the installer no longer generates TLS CA/cert/key material.
    # Keep the helper to avoid breaking external automation that might source it.
    local key="$1" default_value="$2" placeholder="$3"
    local current
    current=$(ensure_general_default "$key" "$default_value")

    if [ -n "$placeholder" ] && [ "$current" = "$placeholder" ]; then
        current="$default_value"
        uci_set_general "$key" "$current"
    fi

    if [ ! -s "$current" ]; then
        current="$default_value"
        uci_set_general "$key" "$current"
    fi

    printf '%s\n' "$current"
}

ensure_secure_serial_secret() {
    ensure_uci_config
    
    local current_secret
    current_secret=$(uci_get_general serial_shared_secret)
    if [ -n "$current_secret" ] \
        && [ "$current_secret" != "$SERIAL_SECRET_PLACEHOLDER" ] \
        && [ "$current_secret" != "$BOOTSTRAP_SERIAL_SECRET" ]; then
        return
    fi

    echo "[INFO] Generating secure serial shared secret via UCI..."
    local final_secret=""
    local rotation_ok=0

    # Try helper if exists
    if command -v yunbridge-rotate-credentials >/dev/null 2>&1; then
        if OUTPUT=$(yunbridge-rotate-credentials 2>&1); then
            rotation_ok=1
            final_secret=$(printf '%s\n' "$OUTPUT" | sed -n 's/^SERIAL_SECRET=//p' | tail -n 1)
        fi
    fi

    if [ "$rotation_ok" -ne 1 ] || [ -z "$final_secret" ]; then
        final_secret=$(generate_random_hex 32)
        local mqtt_pass mqtt_user
        mqtt_pass=$(generate_random_b64 32)
        mqtt_user=$(uci_get_general mqtt_user)
        [ -z "$mqtt_user" ] && mqtt_user="yunbridge"

        uci_set_general serial_shared_secret "$final_secret"
        uci_set_general mqtt_user "$mqtt_user"
        uci_set_general mqtt_pass "$mqtt_pass"
        uci_commit_general
    fi

    local final_current
    final_current=$(uci_get_general serial_shared_secret)
    echo "[INFO] Serial shared secret refreshed in UCI."
}

set_serial_uci_value() {
    local key="$1" default_value="$2"
    ensure_general_default "$key" "$default_value" >/dev/null
}

configure_serial_link_settings() {
    set_serial_uci_value "serial_retry_timeout" "$DEFAULT_SERIAL_RETRY_TIMEOUT"
    set_serial_uci_value "serial_retry_attempts" "$DEFAULT_SERIAL_RETRY_ATTEMPTS"
    set_serial_uci_value "serial_response_timeout" "$DEFAULT_SERIAL_RESPONSE_TIMEOUT"
    set_serial_uci_value "serial_handshake_min_interval" "$DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL"
    set_serial_uci_value "serial_handshake_fatal_failures" "$DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES"
    uci_commit_general
}

#  --- Main Script Execution ---
echo "[STEP 1/6] Checking swap availability..."
swap_total_kb=$(read_swap_total_with_fallback)
echo "[INFO] Detected swap total: ${swap_total_kb} KB"
if [ "${swap_total_kb:-0}" -lt "$MIN_SWAP_KB" ]; then
    if [ "${swap_total_kb:-0}" -eq 0 ] && [ -f /overlay/swapfile ]; then
        if swapon /overlay/swapfile 2>/dev/null; then
            swap_total_kb=$(read_swap_total_with_fallback)
        fi
    fi
    if [ "${swap_total_kb:-0}" -lt "$MIN_SWAP_KB" ]; then
        echo "[ERROR] System swap below 1GB. Run './2_expand.sh' first." >&2
        exit 1
    fi
fi

echo "[STEP 2/6] Checking for conflicting PPP/DHCP packages..."
# [FIX] Conflict removal: tolerate errors if package is missing
CONFLICT_PKGS="ppp ppp-mod-pppoe pppoe odhcp6c odhcpd"
found_conflicts=""
for pkg in $CONFLICT_PKGS; do
    # Check if exact package is installed
    if apk info -e "$pkg" >/dev/null 2>&1; then
        found_conflicts="$found_conflicts $pkg"
    fi
done

if [ -n "$found_conflicts" ]; then
    echo "[WARN] Packages locking serial port found: $found_conflicts"
    printf "Remove these packages? [y/N]: "
    read remove_answer || remove_answer=""
    case "$remove_answer" in
        y|Y)
            echo "[INFO] Removing conflicting packages..."
            # [FIX] Added || true to prevent script exit if a package is not found
            apk del $found_conflicts || true
            ;;
        *)
            echo "[INFO] Keeping existing PPP/DHCP packages." ;;
    esac
fi

stop_daemon

echo "[STEP 3/6] Updating system packages..."
apk update

# [FIX] Removed 'apk upgrade' option entirely to prevent SELinux breakage on RCs
echo "[INFO] Skipping system upgrade to maintain stability."

echo "[STEP 4/6] Installing essential dependencies..."
ESSENTIAL_PACKAGES="\
python3 \
python3-asyncio \
python3-uci \
python3-pyserial \
python3-psutil \
python3-more-itertools \
python3-pip \
openssl-util \
coreutils-stty \
mosquitto-client-ssl \
uhttpd-mod-lua \
luci-base \
luci-compat \
luci-lua-runtime \
luaposix \
luci \
avrdude"

for pkg in $ESSENTIAL_PACKAGES; do
    install_dependency "$pkg"
done

# Ensure we have a Lua runtime
if apk info -e lua >/dev/null 2>&1; then install_dependency "lua";
elif apk info -e lua5.1 >/dev/null 2>&1; then install_dependency "lua5.1"; fi

install_manifest_pip_requirements

# --- Install Prebuilt Packages ---
echo "[STEP 5/6] Installing project .apk packages..."
project_apk_globs=$(uci_get_general installer_project_apk_globs)
[ -z "$project_apk_globs" ] && project_apk_globs="$PROJECT_APK_PATTERNS"
project_apk_installed=0
for glob in $project_apk_globs; do
    # [FIX] Use hyphen to match exact package versions
    for apk in bin/$glob; do
        [ -e "$apk" ] || continue
        pkg_name=$(basename "$apk")
        echo "[INFO] Installing $pkg_name from ./bin"
        # [FIX] Removed --force-reinstall
        if ! apk add $LOCAL_APK_INSTALL_FLAGS "./$apk"; then
            echo "[ERROR] Failed to add $pkg_name from ./bin." >&2
            exit 1
        fi
        project_apk_installed=1
    done
done

if [ "$project_apk_installed" -eq 0 ]; then
    echo "[WARN] No project .apk files found in bin/. 'yunbridge' package was NOT installed."
    echo "[HINT] Run './1_compile.sh' first to build the packages."
else
    # Only configure secrets if the package installed successfully
    ensure_secure_serial_secret
    configure_serial_link_settings
fi

# --- System & LuCI Configuration ---
echo "[STEP 6/6] Finalizing system configuration..."
[ -f /etc/init.d/uhttpd ] && /etc/init.d/uhttpd restart
[ -f /etc/init.d/rpcd ] && /etc/init.d/rpcd restart

echo "[FINAL] Finalizing setup..."

# The daemon is configured via UCI (environment variables are ignored).
if command -v uci >/dev/null 2>&1; then
    uci set yunbridge.general.debug='1' >/dev/null 2>&1 || true
    uci commit yunbridge >/dev/null 2>&1 || true
fi

if [ -x "$INIT_SCRIPT" ]; then
    echo "[INFO] Enabling and starting yunbridge daemon..."
    $INIT_SCRIPT enable
    $INIT_SCRIPT restart
else
    echo "[WARNING] yunbridge init script not found (installation incomplete?)."
fi

echo -e "\n--- Installation Complete! ---"
