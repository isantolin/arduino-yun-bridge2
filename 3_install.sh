#!/bin/sh
set -eu
# This file is part of Arduino Yun Ecosystem v2.
# Copyright (C) 2025 Ignacio Santolin and contributors
# This program is free software: you can redistribute it and/or modify


if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: este script debe ejecutarse como root." >&2
    exit 1
fi

#  --- Configuration Variables ---
INIT_SCRIPT="/etc/init.d/yunbridge"
REQUIRED_SWAP_KB=1048576
MIN_SWAP_KB=$((REQUIRED_SWAP_KB * 99 / 100))
export TMPDIR=/overlay/upper/tmp
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

#  Install essential packages one by one, checking first.
opkg install python3-aio-mqtt-mod python3-asyncio \
    coreutils-stty mosquitto-client-ssl uhttpd-mod-lua \
    luci-base luci-compat luci-lua-runtime luci "$LUA_RUNTIME"

# ANÁLISIS: Se eliminó el bucle 'for pkg in $PACKAGES'
# Era código muerto: $PACKAGES no estaba definido y los paquetes
# ya se instalaron en el comando 'opkg install' anterior.

#  --- Stop Existing Daemon ---
stop_daemon
# --- Install Prebuilt Packages ---
echo "[STEP 5/6] Installing .ipk packages..."
#  Install all .ipk packages from the bin/ directory
if ls bin/*.ipk 1>/dev/null 2>&1; then
    if ! opkg install --force-reinstall bin/*.ipk; then
        echo "[ERROR] La instalación de los paquetes .ipk falló." >&2
        exit 1
    fi
fi

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
read -p "Do you want to enable YUNBRIDGE_DEBUG=1 by default for all users? [Y/n]: " yn
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
