#!/bin/sh
set -eu
# Always run relative paths from the repository root
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"
# Required metadata paths relative to the repository root.
DEPENDENCY_MANIFEST="$PROJECT_ROOT/dependencies/runtime.toml"

# This file is part of Arduino Yun Ecosystem v2.
# Copyright (C) 2025 Ignacio Santolin and contributors
# This program is free software: you can redistribute it and/or modify


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
export TMPDIR=/overlay/upper/tmp
LOCAL_IPK_INSTALL_FLAGS="--force-reinstall --force-downgrade --force-overwrite --force-depends --nodeps"
SERIAL_SECRET_PLACEHOLDER="changeme123"
BOOTSTRAP_SERIAL_SECRET="755142925659b6f5d3ab00b7b280d72fc1cc17f0dad9f52fff9f65efd8caf8e3"
DEFAULT_TLS_DIR="/etc/yunbridge/tls"
DEFAULT_TLS_CAFILE="$DEFAULT_TLS_DIR/ca.crt"
DEFAULT_TLS_CERTFILE="$DEFAULT_TLS_DIR/yunbridge.crt"
DEFAULT_TLS_KEYFILE="$DEFAULT_TLS_DIR/yunbridge.key"
SHIPPING_TLS_CAFILE_PLACEHOLDER="/etc/ssl/certs/ca-certificates.crt"
# Keep shell defaults aligned with yunbridge.const to seed UCI on fresh installs.
DEFAULT_SERIAL_RETRY_TIMEOUT="0.75"
DEFAULT_SERIAL_RESPONSE_TIMEOUT="3.0"
DEFAULT_SERIAL_RETRY_ATTEMPTS="3"
DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL="0.0"
DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES="3"
SKIP_TLS_AUTOGEN="${YUNBRIDGE_SKIP_TLS_AUTOGEN:-0}"
if [ "${YUNBRIDGE_FORCE_TLS_REGEN+set}" = "set" ]; then
    FORCE_TLS_REGEN="$YUNBRIDGE_FORCE_TLS_REGEN"
    FORCE_TLS_REGEN_USER_SET=1
else
    FORCE_TLS_REGEN="1"
    FORCE_TLS_REGEN_USER_SET=0
fi
if [ "$SKIP_TLS_AUTOGEN" = "1" ] && [ "$FORCE_TLS_REGEN_USER_SET" = "0" ]; then
    # Allow YUNBRIDGE_SKIP_TLS_AUTOGEN alone to disable regeneration when user did not override the force flag.
    FORCE_TLS_REGEN="0"
fi
PROJECT_IPK_PATTERNS="\
openwrt-yun-bridge_*.ipk \
openwrt-yun-core_*.ipk \
luci-app-yunbridge_*.ipk"
UCI_GENERAL_DIRTY=0
#  --- Helper Functions ---
mkdir -p "$TMPDIR"
# Function to stop the yunbridge daemon robustly
stop_daemon() {
    if [ ! -x "$INIT_SCRIPT" ]; then
        echo "[INFO] YunBridge daemon not installed, skipping stop."
        return
    fi

    echo "[INFO] Stopping yunbridge daemon if active..."
    # First, try a graceful stop
    $INIT_SCRIPT stop 2>/dev/null || true
    sleep 1

    # Find any remaining yunbridge python processes
    pids=$(ps w | grep -E 'python[0-9.]*.*yunbridge' | grep -v grep | awk '{print $1}')

    if [ -n "$pids" ]; then
        echo "[WARN] Daemon still running. Sending SIGTERM..."
        kill $pids 2>/dev/null || true
        sleep 2 # Give it time to terminate

        # Final check and force kill
        pids2=$(ps w | grep -E 'python[0-9.]*.*yunbridge' | grep -v grep | awk '{print $1}')
        if [ -n "$pids2" ]; then
            echo "[WARN] Process will not die. Sending SIGKILL..."
            kill -9 $pids2 2>/dev/null || true
        fi
    else
        echo "[INFO] No running yunbridge daemon process found."
    fi
}
# Helper to sum swap space (in KB) from /proc/swaps. BusyBox compatible.
read_swap_total_kb() {
    awk 'NR>1 {sum+=$3} END {print sum+0}' /proc/swaps 2>/dev/null
}

# BusyBox swapon (24.x) lacks --show but supports -s; use it when /proc/swaps is empty.
read_swap_total_with_fallback() {
    total=$(read_swap_total_kb)
    if [ "${total:-0}" -eq 0 ]; then
        total=$(swapon -s 2>/dev/null | awk 'NR>1 {sum+=$3} END {print sum+0}')
    fi
    echo "${total:-0}"
}

install_dependency() {
    pkg="$1"
    if opkg list-installed "$pkg" >/dev/null 2>&1; then
        echo "[INFO] Package $pkg already installed."
        return 0
    fi

    local local_ipk=""
    for candidate in "bin/${pkg}"_*.ipk; do
        if [ -f "$candidate" ]; then
            local_ipk="$candidate"
            break
        fi
    done

    if [ -n "$local_ipk" ]; then
        echo "[INFO] Installing $pkg from bundled IPK ($local_ipk)."
        if opkg install $LOCAL_IPK_INSTALL_FLAGS "./$local_ipk"; then
            return 0
        fi
        echo "[WARN] Failed to install $pkg from bundled IPK; trying configured feeds." >&2
    fi

    if opkg install "$pkg"; then
        echo "[INFO] Installed $pkg from configured feeds."
        return 0
    fi

    echo "[ERROR] Failed to install dependency $pkg from feeds or bin/." >&2
    echo "[HINT] Run ./1_compile.sh to refresh local packages or update feeds." >&2
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
        echo "[ERROR] python3 binary not found; install python3 before running this installer." >&2
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

    # Check disk space on overlay before heavy pip install
    if ! check_disk_space "/overlay"; then
        echo "[ERROR] Not enough space for pip packages. Extend your rootfs with ./2_expand.sh" >&2
        rm -f "$tmp_requirements"
        exit 1
    fi

    echo "[INFO] Installing pinned PyPI dependencies defined in dependencies/runtime.toml..."
    if ! python3 -m pip install --no-cache-dir --upgrade --pre -r "$tmp_requirements"; then
        echo "[ERROR] Failed to install pinned PyPI dependencies." >&2
        rm -f "$tmp_requirements"
        exit 1
    fi

    rm -f "$tmp_requirements"
}

uci_get_general() {
    local key="$1"
    uci -q get "yunbridge.general.${key}" 2>/dev/null || true
}

uci_set_general() {
    local key="$1" value="$2"
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

normalize_tls_path_in_uci() {
    local key="$1" default_value="$2" placeholder="$3"
    local current
    current=$(ensure_general_default "$key" "$default_value")

    if [ -n "$placeholder" ] && [ "$current" = "$placeholder" ]; then
        echo "[INFO] Rewriting ${key} placeholder to $default_value for automatic TLS provisioning." >&2
        current="$default_value"
        uci_set_general "$key" "$current"
    fi

    case "$current" in
        $DEFAULT_TLS_DIR/*)
            printf '%s\n' "$current"
            return
            ;;
    esac

    if [ ! -s "$current" ]; then
        echo "[INFO] ${key} path ($current) missing; resetting to $default_value for automatic TLS provisioning." >&2
        current="$default_value"
        uci_set_general "$key" "$current"
    fi

    printf '%s\n' "$current"
}

generate_random_hex() {
    local length="$1" value=""

    if command -v python3 >/dev/null 2>&1; then
        value=$(python3 - "$length" <<'PY'
import binascii
import os
import sys

length = int(sys.argv[1])
print(binascii.hexlify(os.urandom(length)).decode(), end="")
PY
        )
    fi

    if [ -z "$value" ] && command -v hexdump >/dev/null 2>&1; then
        value=$(head -c "$length" /dev/urandom | hexdump -v -e '/1 "%02x"')
    fi

    if [ -z "$value" ] && command -v od >/dev/null 2>&1; then
        value=$(head -c "$length" /dev/urandom | od -An -tx1 | tr -d ' \n')
    fi

    printf '%s\n' "$value"
}

generate_random_b64() {
    local length="$1" value=""

    if command -v python3 >/dev/null 2>&1; then
        value=$(python3 - "$length" <<'PY'
import base64
import os
import sys

length = int(sys.argv[1])
print(base64.b64encode(os.urandom(length)).decode().rstrip('='), end="")
PY
        )
    fi

    if [ -z "$value" ] && command -v base64 >/dev/null 2>&1; then
        value=$(head -c "$length" /dev/urandom | base64 | tr -d '\n=')
    fi

    if [ -z "$value" ]; then
        value=$(generate_random_hex "$length")
    fi

    printf '%s\n' "$value"
}

generate_tls_material() (
    set -e
    local cafile="$1" certfile="$2" keyfile="$3"

    if ! command -v openssl >/dev/null 2>&1; then
        echo "[ERROR] openssl utility not found. Install openssl-util and rerun the installer." >&2
        exit 1
    fi

    local tls_dir cert_dir key_dir ca_key tmpdir serial_file ca_days client_days
    tls_dir=$(dirname "$cafile")
    cert_dir=$(dirname "$certfile")
    key_dir=$(dirname "$keyfile")
    mkdir -p "$tls_dir" "$cert_dir" "$key_dir"
    umask 077
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/yunbridge-tls.XXXXXX")
    trap 'rm -rf "$tmpdir"' EXIT
    ca_key="$tls_dir/ca.key"
    serial_file="$tls_dir/ca.srl"
    ca_days="${YUNBRIDGE_TLS_CA_DAYS:-3650}"
    client_days="${YUNBRIDGE_TLS_CLIENT_DAYS:-825}"

    cat >"$tmpdir/ca.cnf" <<'EOF'
[ req ]
default_bits = 3072
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_ca

[ dn ]
CN = YunBridge Local CA
O = YunBridge
OU = Installer

[ v3_ca ]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical,CA:true
keyUsage = critical, digitalSignature, cRLSign, keyCertSign
EOF

    cat >"$tmpdir/client.cnf" <<'EOF'
[ req ]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
req_extensions = req_ext

[ dn ]
CN = YunBridge Client
O = YunBridge
OU = Installer

[ req_ext ]
extendedKeyUsage = clientAuth
keyUsage = digitalSignature
subjectAltName = DNS:yunbridge-mqtt
EOF

    echo "[INFO] Generating MQTT CA certificate under $tls_dir..."
    openssl req -x509 -config "$tmpdir/ca.cnf" \
        -newkey rsa:3072 \
        -keyout "$ca_key" \
        -out "$cafile" \
        -days "$ca_days" \
        -sha256 \
        -nodes

    echo "[INFO] Generating MQTT client certificate..."
    openssl req -new -config "$tmpdir/client.cnf" \
        -keyout "$keyfile" \
        -out "$tmpdir/client.csr" \
        -nodes

    openssl x509 -req \
        -in "$tmpdir/client.csr" \
        -CA "$cafile" \
        -CAkey "$ca_key" \
        -CAcreateserial \
        -out "$certfile" \
        -days "$client_days" \
        -sha256 \
        -extensions req_ext \
        -extfile "$tmpdir/client.cnf"

    chmod 600 "$cafile" "$ca_key" "$certfile" "$keyfile"
    rm -f "$serial_file"
    echo "[INFO] TLS material created. Import $cafile into your MQTT broker trust store if you're enabling mutual TLS."
)

ensure_tls_material() {
    if [ "$SKIP_TLS_AUTOGEN" = "1" ] && [ "$FORCE_TLS_REGEN" != "1" ]; then
        echo "[INFO] YUNBRIDGE_SKIP_TLS_AUTOGEN=1 detected; skipping TLS material generation."
        return
    fi

    if ! command -v openssl >/dev/null 2>&1; then
        echo "[INFO] openssl binary not found; attempting to install openssl-util dependency."
        install_dependency openssl-util
        if ! command -v openssl >/dev/null 2>&1; then
            echo "[ERROR] openssl is still unavailable after installing openssl-util. Aborting TLS provisioning." >&2
            exit 1
        fi
    fi

    if [ "$FORCE_TLS_REGEN" = "1" ]; then
        echo "[INFO] YUNBRIDGE_FORCE_TLS_REGEN=1: forcing TLS material regeneration."
    fi

    local cafile certfile keyfile
    if [ "$FORCE_TLS_REGEN" = "1" ]; then
        echo "[INFO] Resetting MQTT TLS paths to defaults under $DEFAULT_TLS_DIR."
        cafile="$DEFAULT_TLS_CAFILE"
        certfile="$DEFAULT_TLS_CERTFILE"
        keyfile="$DEFAULT_TLS_KEYFILE"
        uci_set_general mqtt_cafile "$cafile"
        uci_set_general mqtt_certfile "$certfile"
        uci_set_general mqtt_keyfile "$keyfile"
        uci_commit_general
    else
        cafile=$(normalize_tls_path_in_uci mqtt_cafile "$DEFAULT_TLS_CAFILE" "$SHIPPING_TLS_CAFILE_PLACEHOLDER")
        certfile=$(normalize_tls_path_in_uci mqtt_certfile "$DEFAULT_TLS_CERTFILE" "")
        keyfile=$(normalize_tls_path_in_uci mqtt_keyfile "$DEFAULT_TLS_KEYFILE" "")
        uci_commit_general
    fi

    if [ -z "$cafile" ]; then
        echo "[INFO] MQTT CA file not configured; skipping TLS material generation."
        return
    fi

    if [ -z "$certfile" ] && [ -z "$keyfile" ]; then
        echo "[INFO] MQTT client certificate not requested; skipping TLS material generation."
        return
    fi

    if [ -z "$certfile" ] || [ -z "$keyfile" ]; then
        cat >&2 <<EOF
[ERROR] mqtt_certfile/mqtt_keyfile mismatch detected. Ensure both values are set in UCI or remove both to disable client authentication.
EOF
        exit 1
    fi

    case "$cafile" in
        $DEFAULT_TLS_DIR/*) ;;
        *)
            echo "[INFO] MQTT CA path ($cafile) is outside $DEFAULT_TLS_DIR; skipping auto-generation."
            return
            ;;
    esac

    case "$certfile" in
        $DEFAULT_TLS_DIR/*) ;;
        *)
            echo "[INFO] MQTT cert path ($certfile) is outside $DEFAULT_TLS_DIR; skipping auto-generation."
            return
            ;;
    esac

    case "$keyfile" in
        $DEFAULT_TLS_DIR/*) ;;
        *)
            echo "[INFO] MQTT key path ($keyfile) is outside $DEFAULT_TLS_DIR; skipping auto-generation."
            return
            ;;
    esac

    if [ "$FORCE_TLS_REGEN" = "1" ]; then
        echo "[INFO] Removing existing TLS artifacts before regeneration."
        rm -f "$cafile" "$certfile" "$keyfile" "$DEFAULT_TLS_DIR/ca.key" "$DEFAULT_TLS_DIR/ca.srl"
    fi

    if [ -s "$cafile" ] && [ -s "$certfile" ] && [ -s "$keyfile" ]; then
        echo "[INFO] Existing TLS material detected under $DEFAULT_TLS_DIR; skipping auto-generation."
        return
    fi

    echo "[INFO] Provisioning MQTT TLS assets under $DEFAULT_TLS_DIR..."
    generate_tls_material "$cafile" "$certfile" "$keyfile"
}

ensure_secure_serial_secret() {
    local current_secret
    current_secret=$(uci_get_general serial_shared_secret)
    if [ -n "$current_secret" ] \
        && [ "$current_secret" != "$SERIAL_SECRET_PLACEHOLDER" ] \
        && [ "$current_secret" != "$BOOTSTRAP_SERIAL_SECRET" ]; then
        return
    fi

    echo "[INFO] Generating secure serial shared secret via UCI..."
    local rotation_ok=0
    local rotation_output=""
    local final_secret=""

    if command -v yunbridge-rotate-credentials >/dev/null 2>&1; then
        if rotation_output=$(yunbridge-rotate-credentials 2>&1); then
            rotation_ok=1
            printf '%s\n' "$rotation_output"
            final_secret=$(printf '%s\n' "$rotation_output" | sed -n 's/^SERIAL_SECRET=//p' | tail -n 1)
        else
            echo "[WARN] yunbridge-rotate-credentials failed; using local fallback." >&2
        fi
    fi

    if [ "$rotation_ok" -ne 1 ] || [ -z "$final_secret" ]; then
        echo "[INFO] Falling back to local secret/password generation via UCI." >&2
        final_secret=$(generate_random_hex 32)
        if [ -z "$final_secret" ]; then
            echo "[ERROR] Unable to generate a random serial shared secret." >&2
            exit 1
        fi

        local mqtt_pass mqtt_user
        mqtt_pass=$(generate_random_b64 32)
        if [ -z "$mqtt_pass" ]; then
            echo "[ERROR] Unable to generate a random MQTT password." >&2
            exit 1
        fi

        mqtt_user=$(uci_get_general mqtt_user)
        if [ -z "$mqtt_user" ]; then
            mqtt_user="yunbridge"
        fi

        uci_set_general serial_shared_secret "$final_secret"
        uci_set_general mqtt_user "$mqtt_user"
        uci_set_general mqtt_pass "$mqtt_pass"
        uci_commit_general
    fi

    local final_current
    final_current=$(uci_get_general serial_shared_secret)
    if [ -z "$final_current" ] \
        || [ "$final_current" = "$SERIAL_SECRET_PLACEHOLDER" ] \
        || [ "$final_current" = "$BOOTSTRAP_SERIAL_SECRET" ]; then
        echo "[ERROR] Unable to read serial shared secret from UCI after provisioning." >&2
        exit 1
    fi

    cat <<EOF
[INFO] Serial shared secret refreshed in UCI.
[HINT] Paste the following into your sketches before including Bridge.h:
       #define BRIDGE_SERIAL_SHARED_SECRET "$final_current"
EOF
}

set_serial_uci_value() {
    local key="$1" default_value="$2" env_value="$3"
    if [ -n "$env_value" ]; then
        echo "[INFO] Applying ${key} override from environment: $env_value"
        uci_set_general "$key" "$env_value"
    else
        ensure_general_default "$key" "$default_value" >/dev/null
    fi
}

configure_serial_link_settings() {
    echo "[INFO] Ensuring serial timing defaults in UCI..."
    set_serial_uci_value "serial_retry_timeout" \
        "$DEFAULT_SERIAL_RETRY_TIMEOUT" "${YUNBRIDGE_SERIAL_RETRY_TIMEOUT:-}"
    set_serial_uci_value "serial_retry_attempts" \
        "$DEFAULT_SERIAL_RETRY_ATTEMPTS" "${YUNBRIDGE_SERIAL_RETRY_ATTEMPTS:-}"
    set_serial_uci_value "serial_response_timeout" \
        "$DEFAULT_SERIAL_RESPONSE_TIMEOUT" "${YUNBRIDGE_SERIAL_RESPONSE_TIMEOUT:-}"
    set_serial_uci_value "serial_handshake_min_interval" \
        "$DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL" "${YUNBRIDGE_SERIAL_HANDSHAKE_MIN_INTERVAL:-}"
    set_serial_uci_value "serial_handshake_fatal_failures" \
        "$DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES" "${YUNBRIDGE_SERIAL_HANDSHAKE_FATAL_FAILURES:-}"
    uci_commit_general
}
#  --- Main Script Execution ---
echo "[STEP 1/6] Checking swap availability..."
swap_total_kb=$(read_swap_total_with_fallback)
echo "[INFO] Detected swap total: ${swap_total_kb} KB"
if [ "${swap_total_kb:-0}" -lt "$MIN_SWAP_KB" ]; then
    if [ "${swap_total_kb:-0}" -eq 0 ] && [ -f /overlay/swapfile ]; then
        current_bytes=$(stat -c%s /overlay/swapfile 2>/dev/null || echo 0)
        if [ "$current_bytes" -ge 1073741824 ]; then
            echo "[INFO] Found 1GB swapfile on disk but not active; enabling now."
            if swapon /overlay/swapfile 2>/dev/null; then
                swap_total_kb=$(read_swap_total_with_fallback)
            else
                echo "[WARN] Failed to enable /overlay/swapfile automatically." >&2
            fi
        fi
    fi

    if [ "${swap_total_kb:-0}" -lt "$MIN_SWAP_KB" ]; then
        cat >&2 <<'EOF'
[ERROR] System swap below 1GB. Run './2_expand.sh' first to provision extroot + swap, confirm with 'free -h', then rerun this installer.
EOF
        exit 1
    fi
fi

echo "[STEP 2/6] Checking for conflicting PPP/DHCP packages..."
CONFLICT_PKGS="ppp ppp-mod-pppoe pppoe odhcp6c odhcpd"
found_conflicts=""
for pkg in $CONFLICT_PKGS; do
    if opkg list-installed "$pkg" >/dev/null 2>&1; then
        found_conflicts="$found_conflicts $pkg"
    fi
done

REMOVE_CONFLICTS_SETTING="${YUNBRIDGE_REMOVE_PPP:-prompt}"
case "$REMOVE_CONFLICTS_SETTING" in
    1|true|TRUE|yes|YES)
        REMOVE_CONFLICTS_SETTING="auto-remove"
        ;;
    0|false|FALSE|no|NO)
        REMOVE_CONFLICTS_SETTING="skip"
        ;;
esac

if [ -n "$found_conflicts" ]; then
    echo "[WARN] The following packages can lock the serial port:$found_conflicts"
    if [ "$REMOVE_CONFLICTS_SETTING" = "auto-remove" ]; then
        echo "[INFO] YUNBRIDGE_REMOVE_PPP signals automatic removal."
        opkg remove $found_conflicts --force-depends || true
    elif [ "$REMOVE_CONFLICTS_SETTING" = "skip" ]; then
        echo "[INFO] Skipping removal as requested via YUNBRIDGE_REMOVE_PPP=0."
    else
        printf "Do you want to remove these packages now? [y/N]: "
        read remove_answer || remove_answer=""
        case "$remove_answer" in
            y|Y)
                opkg remove $found_conflicts --force-depends || true
                ;;
            *)
                echo "[INFO] Keeping existing PPP/DHCP packages. Ensure ttyATH0 is free before running YunBridge." ;;
        esac
    fi
else
    echo "[INFO] No conflicting packages detected."
fi

echo "[STEP 3/6] Updating system packages..."
opkg update

AUTO_UPGRADE="${YUNBRIDGE_AUTO_UPGRADE:-0}"
if [ "$AUTO_UPGRADE" = "1" ]; then
    echo "[INFO] YUNBRIDGE_AUTO_UPGRADE=1: ejecutando opkg upgrade sin prompt."
    opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade
else
    printf "¿Deseas ejecutar 'opkg upgrade' para todos los paquetes? [y/N]: "
    read upgrade_answer || upgrade_answer=""
    case "$upgrade_answer" in
        y|Y)
            opkg list-upgradable | cut -f 1 -d ' ' | xargs -r opkg upgrade
            ;;
        *)
            echo "[INFO] Se omitió 'opkg upgrade'." ;;
    esac
fi

echo "[STEP 4/6] Installing essential dependencies..."
#  Determine Lua runtime package name (varies across OpenWrt releases).
if opkg info lua >/dev/null 2>&1; then
    LUA_RUNTIME="lua"
elif opkg info lua5.1 >/dev/null 2>&1; then
    LUA_RUNTIME="lua5.1"
else
    echo "[ERROR] No Lua runtime package (lua or lua5.1) available in opkg feeds." >&2
    echo "[HINT] Ensure the base and packages feeds are up to date before rerunning this installer." >&2
    exit 1
fi

#  Install essential packages with local fallbacks when feeds lack them.
ESSENTIAL_PACKAGES="\
python3-asyncio \
python3-uci \
python3-pyserial \
python3-psutil \
python3-more-itertools \
openssl-util \
coreutils-stty \
mosquitto-client-ssl \
uhttpd-mod-lua \
luci-base \
luci-compat \
luci-lua-runtime \
luaposix \
luci \
${LUA_RUNTIME}"

for pkg in $ESSENTIAL_PACKAGES; do
    install_dependency "$pkg"
done

install_manifest_pip_requirements

#  --- Stop Existing Daemon ---
stop_daemon
# --- Install Prebuilt Packages ---
echo "[STEP 5/6] Installing project .ipk packages..."
project_ipk_globs=${YUNBRIDGE_PROJECT_IPK_GLOBS:-$PROJECT_IPK_PATTERNS}
project_ipk_installed=0
for glob in $project_ipk_globs; do
    for ipk in bin/$glob; do
        [ -e "$ipk" ] || continue
        pkg_name=$(basename "$ipk")
        echo "[INFO] Installing $pkg_name from ./bin"
        if ! opkg install $LOCAL_IPK_INSTALL_FLAGS "./$ipk"; then
            echo "[ERROR] Failed to install $pkg_name from ./bin." >&2
            exit 1
        fi
        project_ipk_installed=1
    done
done

if [ "$project_ipk_installed" -eq 0 ]; then
    echo "[INFO] No project-specific .ipk files found in bin/. Skipping Step 5."
fi

ensure_secure_serial_secret
ensure_tls_material
configure_serial_link_settings

# --- System & LuCI Configuration ---
echo "[STEP 6/6] Finalizing system configuration..."
#  Remove stale Lua prefix if present on modern LuCI releases.
if [ -f /etc/config/uhttpd ]; then
    raw_prefix=$(uci -q get uhttpd.main.lua_prefix || true)
    if [ -n "${raw_prefix:-}" ]; then
        lua_entry=${raw_prefix#*=}
        if [ -z "$lua_entry" ]; then
            echo "[INFO] Clearing legacy lua_prefix from /etc/config/uhttpd (empty entry)."
            uci -q del uhttpd.main.lua_prefix || true
            uci commit uhttpd
        elif [ ! -f "$lua_entry" ]; then
            echo "[INFO] Clearing legacy lua_prefix from /etc/config/uhttpd (missing file: $lua_entry)."
            uci -q del uhttpd.main.lua_prefix || true
            uci commit uhttpd
        fi
    fi

    if ! uci -q get uhttpd.main.ucode_prefix >/dev/null; then
        echo "[INFO] Ensuring LuCI ucode handler is registered with uhttpd."
        uci add_list uhttpd.main.ucode_prefix='/cgi-bin/luci=/usr/share/ucode/luci/uhttpd.uc'
        uci commit uhttpd
        [ -x /etc/init.d/uhttpd ] && /etc/init.d/uhttpd reload
    fi
fi
#  Remove serial console login to free up the port for the bridge
if grep -q '::askconsole:/usr/libexec/login.sh' /etc/inittab; then
    echo "[INFO] Removing serial console login from /etc/inittab."
    sed -i '/::askconsole:\/usr\/libexec\/login.sh/d' /etc/inittab
fi
#  Restart services to apply changes and load the new LuCI app
echo "[INFO] Restarting uhttpd and rpcd for LuCI..."
[ -f /etc/init.d/uhttpd ] && /etc/init.d/uhttpd restart
[ -f /etc/init.d/rpcd ] && /etc/init.d/rpcd restart
#  --- User Configuration & Daemon Start ---
echo "[FINAL] Finalizing setup..."
#  Ask user if they want to enable debug mode by default
printf "Do you want to enable YUNBRIDGE_DEBUG=1 by default for all users? [Y/n]: "
read yn || yn=""
case $yn in
    [Nn])
        echo "[INFO] YUNBRIDGE_DEBUG will not be set by default."
        ;;
    *)
        mkdir -p /etc/profile.d
        echo "export YUNBRIDGE_DEBUG=1" > /etc/profile.d/yunbridge_debug.sh
        chmod +x /etc/profile.d/yunbridge_debug.sh
        echo "[INFO] YUNBRIDGE_DEBUG=1 will be set for all users on login."
        export YUNBRIDGE_DEBUG=1 # Export for current session
        ;;
esac
#  Enable and start the daemon
if [ -x "$INIT_SCRIPT" ]; then
    echo "[INFO] Enabling and starting yunbridge daemon..."
    $INIT_SCRIPT enable
    $INIT_SCRIPT restart
else
    echo "[WARNING] yunbridge init script not found at $INIT_SCRIPT." >&2
fi

echo -e "\n--- Installation Complete! ---"
echo "The YunBridge daemon is now running."
echo "You can configure it from the LuCI web interface under 'Services' > 'YunBridge'."
echo "A reboot is recommended if you encounter any issues."