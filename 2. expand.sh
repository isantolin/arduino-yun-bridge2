#!/bin/sh
set -e
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
SWAP_SIZE_MB=${1:-1024}         # Size to create, default 1024 MB
SWAP_EXPECTED_KB=$((SWAP_SIZE_MB * 1024))
SWAP_FILE_PATH="/swapfile"
# ----------------------------------

# 1. Find the SD card device dynamically
echo "--- Starting Extroot and SWAP Script (Robust Mode) ---" | tee -a $LOG_FILE
echo "Attempting to find SD card device..." | tee -a $LOG_FILE

# List all block devices, filter out internal flash (mmcblk0) and loop devices
# Prioritize devices that are not mmcblk0 and have at least one partition
# This heuristic tries to find common SD card names like mmcblk1 or sda
DETECTED_DEVICE=$(ls -l /sys/block/ | grep -E 'mmcblk[0-9]|sd[a-z]' | awk '{print $9}' | grep -v 'mmcblk0' | grep -v 'loop' | head -n 1)

if [ -z "$DETECTED_DEVICE" ]; then
    echo "ERROR! Could not automatically find SD card device. Please ensure it's inserted." | tee -a $LOG_FILE
    echo "You may need to manually edit this script to set the 'DEVICE' variable, e.g., DEVICE="/dev/mmcblk1" or DEVICE="/dev/sda"." | tee -a $LOG_FILE
    exit 1
fi

DEVICE="/dev/$DETECTED_DEVICE"
echo "Identified potential SD card device: $DEVICE" | tee -a $LOG_FILE

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
    opkg install block-mount kmod-fs-ext4 e2fsprogs util-linux-mountpoint 2>&1 | tee -a $LOG_FILE

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

    # 2.2 DEVICE PREPARATION AND FORMATTING
    echo "   2.2 Unmounting and formatting $DEVICE to ext4..." | tee -a $LOG_FILE
    umount $DEVICE 2>/dev/null
    mkfs.ext4 -F -L extroot $DEVICE 2>&1 | tee -a $LOG_FILE

    if [ $? -ne 0 ]; then
        echo "ERROR! Formatting of $DEVICE failed. Aborting." | tee -a $LOG_FILE
        exit 1
    fi

    # 2.3 EXTROOT CONFIGURATION (FSTAB)
    echo "   2.3 Configuring /etc/config/fstab for the new overlay..." | tee -a $LOG_FILE
    UUID=$(block info $DEVICE | grep -o -e 'UUID="[^\"]*"' | sed 's/UUID="//;s/"//')
    TARGET_MOUNT="/overlay"

    uci -q delete fstab.extroot
    uci set fstab.extroot="mount"
    uci set fstab.extroot.uuid="${UUID}"
    uci set fstab.extroot.target="${TARGET_MOUNT}"
    uci set fstab.extroot.enabled='1'

    # 2.4 ORIGINAL OVERLAY CONFIGURATION (FALLBACK)
    echo "   2.4 Configuring the original overlay for fallback at /rwm..." | tee -a $LOG_FILE
    ORIG_DEVICE=$(block info | sed -n -e '/MOUNT=".*\/overlay"/s/:.*$//p')

    uci -q delete fstab.rwm
    uci set fstab.rwm="mount"
    uci set fstab.rwm.device="${ORIG_DEVICE}"
    uci set fstab.rwm.target="/rwm"

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


# 3. SWAP CONFIGURATION (Verification and Creation)
echo "3. Verifying SWAP configuration..." | tee -a $LOG_FILE
if check_swap_size; then
    # SWAP already configured correctly
    :
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
    dd if=/dev/zero of=${SWAP_TEMP_DIR}${SWAP_FILE_PATH} bs=1M count=${SWAP_SIZE_MB} 2>&1 | tee -a $LOG_FILE
    mkswap ${SWAP_TEMP_DIR}${SWAP_FILE_PATH} 2>&1 | tee -a $LOG_FILE

    # 3.3 Configure the SWAP file in /etc/config/fstab
    uci -q delete fstab.swap_file
    uci set fstab.swap_file="swap"
    uci set fstab.swap_file.device="/overlay${SWAP_FILE_PATH}"
    uci set fstab.swap_file.enabled='1'

    # Unmount if temporarily mounted
    if [ "$SWAP_TEMP_DIR" = "/mnt/swap_temp" ]; then
        sync
        umount /mnt/swap_temp 2>/dev/null
        rmdir /mnt/swap_temp 2>/dev/null
    fi
fi

# 4. SAVE AND REBOOT
echo "4. Saving final configuration and rebooting..." | tee -a $LOG_FILE
uci commit fstab

if [ $? -ne 0 ]; then
    echo "ERROR! Failed to commit fstab changes. Aborting." | tee -a $LOG_FILE
    exit 1
fi

echo "   Configurations verified/updated. System will reboot in 5 seconds." | tee -a $LOG_FILE
echo "   After reboot, run 'df -h' and 'free' to verify the final status." | tee -a $LOG_FILE
sleep 5
reboot