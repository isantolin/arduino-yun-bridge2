#!/bin/sh
set -eu
# shellcheck disable=SC3043 # BusyBox ash no soporta set -o pipefail
#
# OpenWrt Extroot and SWAP Automation Script (Robust Mode)
# *** ROBUST: Verifies both Extroot and SWAP configuration and size. ***
# Uses the /dev/sda1 partition as the new /overlay and creates a 1GB SWAP file.
#
# WARNING! If Extroot is NOT configured or is too small, this script will reformat /dev/sda1.
#

# --- CONFIGURATION VARIABLES ---
# DEVICE="<detected_sd_device>" # This will be determined dynamically
MOUNT_POINT="/mnt/extroot_temp"
LOG_FILE="/var/log/extroot_script.log"
# Expected Sizes
MIN_OVERLAY_KB=102400     # Minimum 100 MB to confirm external SD
SWAP_SIZE_MB=${1:-1024}        # Size to create, default 1024 MB (1 GB)
SWAP_EXPECTED_KB=$((SWAP_SIZE_MB * 1024))
SWAP_FILE_PATH="/swapfile"
# ----------------------------------

ensure_swap_uci_entry() {
    uci -q delete fstab.swap_file || true
    uci set fstab.swap_file="swap"
    uci set fstab.swap_file.device="/overlay${SWAP_FILE_PATH}"
    uci set fstab.swap_file.enabled='1'
}

# Exigir privilegios de root.
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: este script debe ejecutarse como root." >&2
    exit 1
fi

FORCE_FORMAT="${EXTROOT_FORCE:-0}"

# 1. Find the SD card device dynamically
echo "--- Starting Extroot and SWAP Script (Robust Mode) ---" | tee -a $LOG_FILE

if [ -n "${DEVICE:-}" ]; then
    echo "[INFO] DEVICE predefinido detectado: $DEVICE" | tee -a $LOG_FILE
else
    echo "Attempting to find SD card device..." | tee -a $LOG_FILE

    # List all block devices, filter out internal flash (mmcblk0) and loop devices
    DETECTED_DEVICE=$(ls -l /sys/block/ | awk '{print $9}' | grep -E '^mmcblk[0-9]+$|^sd[a-z]+$' | grep -v 'mmcblk0' | head -n 1)

    if [ -z "$DETECTED_DEVICE" ]; then
        echo "ERROR! Could not automatically find SD card device. Please ensure it's inserted." | tee -a $LOG_FILE
        echo "Defina DEVICE=/dev/xxx y ejecute nuevamente." | tee -a $LOG_FILE
        exit 1
    fi

    # Try to find the first partition of the detected device
    PARTITION=$(ls /sys/block/$DETECTED_DEVICE/ | grep -E "^${DETECTED_DEVICE}p?[0-9]" | head -n 1)

    if [ -n "$PARTITION" ]; then
        DEVICE="/dev/$PARTITION"
        echo "Identified SD card partition: $DEVICE" | tee -a $LOG_FILE
    else
        DEVICE="/dev/$DETECTED_DEVICE"
        echo "Identified potential SD card device: $DEVICE. No partition found, using raw device." | tee -a $LOG_FILE
    fi
fi

case "$DEVICE" in
    /dev/mmcblk[1-9]*|/dev/mmcblk[1-9]*p[0-9]*|/dev/sd[a-z][0-9]*) ;;
    *)
        echo "ERROR! El dispositivo detectado ($DEVICE) no corresponde a una tarjeta SD soportada." | tee -a $LOG_FILE
        echo "Defina la variable DEVICE manualmente y vuelva a ejecutar o exporte EXTROOT_FORCE=1 junto a DEVICE=/dev/..." | tee -a $LOG_FILE
        exit 1
        ;;
esac

# --- VERIFICATION FUNCTIONS ---

# Checks if Extroot is active and has the expected size (external SD)
check_extroot_size() {
    # 1. Check if the partition is mounted on /overlay
    if df -k | grep -q "$DEVICE.*\/overlay"; then
        # 2. If mounted, extract the total size in KB
        SIZE_KB=$(df -k | grep "$DEVICE" | awk '{print $2}')
        
        # 3. Check if the total size is larger than the expected minimum (100MB)
        if [ "$SIZE_KB" -gt $MIN_OVERLAY_KB ]; then
            echo "   [OK] Extroot is active and large enough (${SIZE_KB} KB > ${MIN_OVERLAY_KB} KB)." | tee -a $LOG_FILE
            return 0 # Success: Extroot is correctly configured
        else
            echo "   [FAIL] Extroot is active, but size is too small (${SIZE_KB} KB). Will reconfigure." | tee -a $LOG_FILE
            return 1 # Fail: Extroot is too small (might be internal flash)
        fi
    fi
    echo "   [FAIL] Extroot is not active on $DEVICE." | tee -a $LOG_FILE
    return 1 # Fail: Extroot is not active
}

# Checks if the SWAP file is active and has the 1 GB size
check_swap_size() {
    if cat /proc/swaps | grep -q "$SWAP_FILE_PATH"; then
        # Extract the current SWAP size in KB (3rd column of /proc/swaps)
        SWAP_CURRENT_KB=$(cat /proc/swaps | grep "$SWAP_FILE_PATH" | awk '{print $3}')
        
        # Check that the size is at least 99% of the expected size (small tolerance)
        if [ "$SWAP_CURRENT_KB" -ge $((SWAP_EXPECTED_KB * 99 / 100)) ]; then
            echo "   [OK] SWAP is active and has the expected size (${SWAP_CURRENT_KB} KB)." | tee -a $LOG_FILE
            return 0 # Success: SWAP is correctly configured
        else
            echo "   [FAIL] SWAP is active, but size is incorrect (${SWAP_CURRENT_KB} KB). Will reconfigure." | tee -a $LOG_FILE
            return 1 # Fail: SWAP has the wrong size
        fi
    fi
    echo "   [FAIL] SWAP is not active." | tee -a $LOG_FILE
    return 1 # Fail: SWAP is not active
}


# --- START PROCESS ---

# 1. INSTALL REQUIRED PACKAGES
echo "1. Checking and installing required packages..." | tee -a $LOG_FILE

# Revisión corregida: Comprueba la herramienta más importante (mkfs.ext4)
if ! command -v mkfs.ext4 > /dev/null; then
    echo "   [FAIL] mkfs.ext4 not found. Installing e2fsprogs and dependencies..." | tee -a $LOG_FILE
    opkg update 2>&1 | tee -a $LOG_FILE
    opkg install block-mount kmod-fs-ext4 e2fsprogs mount-utils parted kmod-usb-storage 2>&1 | tee -a $LOG_FILE

    if [ $? -ne 0 ]; then
        echo "ERROR! Package installation failed. Aborting." | tee -a $LOG_FILE
        exit 1
    fi
else
    echo "   [OK] Required packages (e2fsprogs) are already installed." | tee -a $LOG_FILE
fi

# 2. EXTROOT CONFIGURATION (Steps 2.1 to 2.6)
echo "2. Verifying Extroot configuration..." | tee -a $LOG_FILE
if check_extroot_size; then
    # Extroot already configured correctly
    : 
else
    echo "2.1 Extroot requires configuration. Proceeding to format and configure..." | tee -a $LOG_FILE

    if [ "$FORCE_FORMAT" != "1" ]; then
        printf "CONFIRM: se formateará %s y se configurará como overlay. ¿Continuar? [y/N]: " "$DEVICE"
        read answer || answer=""
        case "$answer" in
            y|Y)
                echo "  >> Continuando con el formateo." | tee -a $LOG_FILE
                ;;
            *)
                echo "Operación cancelada por el usuario." | tee -a $LOG_FILE
                exit 0
                ;;
        esac
    else
        echo "[INFO] EXTROOT_FORCE=1 detectado, omitiendo confirmación interactiva." | tee -a $LOG_FILE
    fi

    # 2.2 DEVICE PREPARATION AND FORMATTING
    echo "   2.2 Unmounting and formatting $DEVICE to ext4..." | tee -a $LOG_FILE
    echo "   Attempting to unmount $DEVICE (errors will be displayed)..." | tee -a $LOG_FILE
    umount $DEVICE 2>&1 | tee -a $LOG_FILE || echo "   (Ignoring unmount error, proceeding with format)" | tee -a $LOG_FILE
    echo "   Running mkfs.ext4..." | tee -a $LOG_FILE
    mkfs.ext4 -F -L extroot $DEVICE 2>&1 | tee -a $LOG_FILE

    if [ $? -ne 0 ]; then
        echo "ERROR! Formatting of $DEVICE failed. Aborting." | tee -a $LOG_FILE
        exit 1
    fi

    # 2.3 EXTROOT CONFIGURATION (FSTAB)
    echo "   2.3 Configuring /etc/config/fstab for the new overlay..." | tee -a $LOG_FILE
    echo "   Running 'block info $DEVICE' to get UUID..." | tee -a $LOG_FILE
    block info $DEVICE 2>&1 | tee -a $LOG_FILE
    UUID=$(block info $DEVICE | grep -o -e 'UUID="[^\"]*"' | sed 's/UUID="//;s/"//')
    echo "   Extracted UUID: '$UUID'" | tee -a $LOG_FILE

    if [ -z "$UUID" ]; then
        echo "ERROR: Could not extract UUID from $DEVICE after formatting. Aborting." | tee -a $LOG_FILE
        exit 1
    fi

    TARGET_MOUNT="/overlay"

    echo "   Updating fstab with uci..." | tee -a $LOG_FILE
    uci delete fstab.extroot 2>&1 | tee -a $LOG_FILE || echo "   (Could not delete fstab.extroot, probably did not exist)" | tee -a $LOG_FILE
    uci set fstab.extroot="mount" 2>&1 | tee -a $LOG_FILE
    uci set fstab.extroot.uuid="${UUID}" 2>&1 | tee -a $LOG_FILE
    uci set fstab.extroot.target="${TARGET_MOUNT}" 2>&1 | tee -a $LOG_FILE
    uci set fstab.extroot.enabled='1' 2>&1 | tee -a $LOG_FILE
    uci set fstab.extroot.check_fs='1' 2>&1 | tee -a $LOG_FILE
    echo "   fstab update with uci finished." | tee -a $LOG_FILE

    # 2.4 ORIGINAL OVERLAY CONFIGURATION (FALLBACK)
    echo "   2.4 Configuring the original overlay for fallback at /rwm..." | tee -a $LOG_FILE
    echo "   Getting original overlay device..." | tee -a $LOG_FILE
    block info 2>&1 | tee -a $LOG_FILE
    ORIG_DEVICE=$(block info | sed -n -e '/MOUNT=".*\/overlay"/s/:.*$//p')
    echo "   Original overlay device: '$ORIG_DEVICE'" | tee -a $LOG_FILE

    if [ -z "$ORIG_DEVICE" ]; then
        echo "WARNING: Could not determine original overlay device. Skipping fallback configuration." | tee -a $LOG_FILE
    else
        uci -q delete fstab.rwm || true
        uci set fstab.rwm="mount"
        uci set fstab.rwm.device="${ORIG_DEVICE}"
        uci set fstab.rwm.target="/rwm"
    fi

    # 2.5 DATA TRANSFER
    echo "   2.5 Creating temporary mount point and copying data..." | tee -a $LOG_FILE
    mkdir -p $MOUNT_POINT 2>&1 | tee -a $LOG_FILE
    mount $DEVICE $MOUNT_POINT 2>&1 | tee -a $LOG_FILE

    if [ $? -ne 0 ]; then
        echo "ERROR! Temporary mounting of $DEVICE failed. Aborting." | tee -a $LOG_FILE
        exit 1
    fi

    tar -C /overlay -cvf - . | tar -C $MOUNT_POINT -xf - 2>&1 | tee -a $LOG_FILE

    # 2.6 Cleanup
    echo "   2.6 Cleaning up and unmounting data copy..." | tee -a $LOG_FILE
    sync
    umount $MOUNT_POINT
    rmdir $MOUNT_POINT 2>/dev/null
fi

echo "   2.7 Saving Extroot configuration..." | tee -a $LOG_FILE
uci commit fstab
if [ $? -ne 0 ]; then
    echo "ERROR! Failed to commit Extroot fstab changes. Aborting." | tee -a $LOG_FILE
    exit 1
fi


# 3. SWAP CONFIGURATION (Verification and Creation)
echo "3. Verifying SWAP configuration..." | tee -a $LOG_FILE
if check_swap_size; then
    ensure_swap_uci_entry
else
    echo "3.1 SWAP requires configuration. Proceeding to create the ${SWAP_SIZE_MB}MB file..." | tee -a $LOG_FILE

    # Mount the partition if /overlay is not mounted (unlikely if Extroot passed, but safe)
    if ! grep -q ' /overlay ' /proc/mounts; then
        echo "   Temporarily mounting $DEVICE to /mnt/swap_temp to create SWAP file." | tee -a $LOG_FILE
        mkdir -p /mnt/swap_temp 2>&1
        mount $DEVICE /mnt/swap_temp 2>&1 | tee -a $LOG_FILE
        SWAP_TEMP_DIR="/mnt/swap_temp"
    else
        SWAP_TEMP_DIR="/overlay"
    fi

    # 3.2 Create and configure the SWAP file
    echo "   Creating and configuring the SWAP file..." | tee -a $LOG_FILE
    SWAP_TARGET="${SWAP_TEMP_DIR}${SWAP_FILE_PATH}"
    dd if=/dev/zero of="$SWAP_TARGET" bs=1M count=${SWAP_SIZE_MB} 2>&1 | tee -a $LOG_FILE
    mkswap "$SWAP_TARGET" 2>&1 | tee -a $LOG_FILE

    if [ "$SWAP_TEMP_DIR" = "/overlay" ]; then
        echo "   Activating SWAP file immediately..." | tee -a $LOG_FILE
        if swapon "$SWAP_TARGET" >> $LOG_FILE 2>&1; then
            echo "   SWAP activation succeeded." | tee -a $LOG_FILE
        else
            echo "ERROR! Failed to enable SWAP on $SWAP_TARGET. See $LOG_FILE for details." | tee -a $LOG_FILE
            exit 1
        fi
    else
        echo "   SWAP will be enabled automatically after reboot when /overlay is mounted." | tee -a $LOG_FILE
    fi

    # 3.3 Configure the SWAP file in /etc/config/fstab
    ensure_swap_uci_entry

    # Unmount if temporarily mounted
    if [ "$SWAP_TEMP_DIR" = "/mnt/swap_temp" ]; then
        sync
        umount /mnt/swap_temp 2>/dev/null
        rmdir /mnt/swap_temp 2>/dev/null
    fi
fi

echo "   3.4 Saving SWAP configuration..." | tee -a $LOG_FILE
uci commit fstab
if [ $? -ne 0 ]; then
    echo "ERROR! Failed to commit SWAP fstab changes. Aborting." | tee -a $LOG_FILE
    exit 1
fi

# 4. Ensure fstab init script persists swap/overlay on boot
if [ -x /etc/init.d/fstab ]; then
    echo "4. Ensuring fstab init script is enabled for future boots..." | tee -a $LOG_FILE
    if /etc/init.d/fstab enabled >/dev/null 2>&1; then
        echo "   [OK] fstab service already enabled." | tee -a $LOG_FILE
    else
        if /etc/init.d/fstab enable >> $LOG_FILE 2>&1; then
            echo "   [OK] Enabled fstab service for autostart." | tee -a $LOG_FILE
        else
            echo "   [WARN] Could not enable fstab service automatically. Enable manually if needed." | tee -a $LOG_FILE
        fi
    fi

    if /etc/init.d/fstab start >> $LOG_FILE 2>&1; then
        echo "   [OK] fstab service started to validate configuration." | tee -a $LOG_FILE
    else
        echo "   [WARN] Failed to start fstab service. SWAP might need manual 'swapon' after reboot." | tee -a $LOG_FILE
    fi
else
    echo "4. [WARN] /etc/init.d/fstab not found; cannot ensure persistence via init script." | tee -a $LOG_FILE
fi

# 5. REBOOT
echo "5. Configuration saved. System will reboot in 5 seconds." | tee -a $LOG_FILE
echo "   After reboot, run 'df -h' and 'free' to verify the final status." | tee -a $LOG_FILE
echo "   Antes de ejecutar ./3_install.sh puedes exportar" | tee -a $LOG_FILE
echo "     YUNBRIDGE_SERIAL_RETRY_TIMEOUT / YUNBRIDGE_SERIAL_RETRY_ATTEMPTS" | tee -a $LOG_FILE
echo "   para ajustar el control de flujo serie que el instalador aplicará." | tee -a $LOG_FILE
sleep 5
reboot