#!/bin/sh
#
# OpenWrt Extroot and SWAP Automation Script (Arduino Yun)
# Uses the /dev/sda1 partition as the new /overlay and creates a 1GB SWAP file.
#
# WARNING! This script will reformat /dev/sda1 and erase all its data.
#

# --- CONFIGURATION VARIABLES ---
DEVICE="/dev/sda1"
MOUNT_POINT="/mnt/extroot_temp"
LOG_FILE="/tmp/extroot_script.log"
SWAP_SIZE_MB=1024 # 1 GB
SWAP_FILE_PATH="/swapfile"
# -------------------------------

echo "--- Starting Extroot and SWAP Automation Script ---" | tee -a $LOG_FILE
echo "Device to be used: $DEVICE" | tee -a $LOG_FILE

# 1. INSTALL REQUIRED PACKAGES
echo "1. Installing necessary packages (block-mount, kmod-fs-ext4, e2fsprogs)..." | tee -a $LOG_FILE
opkg update 2>&1 | tee -a $LOG_FILE
opkg install block-mount kmod-fs-ext4 e2fsprogs 2>&1 | tee -a $LOG_FILE

if [ $? -ne 0 ]; then
    echo "ERROR! Package installation failed. Aborting." | tee -a $LOG_FILE
    exit 1
fi

# 2. DEVICE PREPARATION AND FORMATTING
echo "2. Unmounting and formatting $DEVICE to ext4..." | tee -a $LOG_FILE
umount $DEVICE 2>/dev/null
mkfs.ext4 -F -L extroot $DEVICE 2>&1 | tee -a $LOG_FILE

if [ $? -ne 0 ]; then
    echo "ERROR! Formatting of $DEVICE failed. Aborting." | tee -a $LOG_FILE
    exit 1
fi

# 3. EXTROOT CONFIGURATION (FSTAB)
echo "3. Configuring /etc/config/fstab for the new overlay..." | tee -a $LOG_FILE

# Get the device's UUID
UUID=$(block info $DEVICE | grep -o -e 'UUID="[^\"]*"' | sed 's/UUID="//;s/"//')
TARGET_MOUNT="/overlay"

# Create entry for the new overlay
uci -q delete fstab.extroot
uci set fstab.extroot="mount"
uci set fstab.extroot.uuid="${UUID}"
uci set fstab.extroot.target="${TARGET_MOUNT}"
uci set fstab.extroot.enabled='1'

# 4. ORIGINAL OVERLAY CONFIGURATION (FALLBACK)
echo "4. Configuring the original overlay for fallback at /rwm..." | tee -a $LOG_FILE

# Get the device name of the original overlay partition
ORIG_DEVICE=$(block info | sed -n -e '/MOUNT=".*\/overlay"/s/:.*$//p')

# Create entry for /rwm
uci -q delete fstab.rwm
uci set fstab.rwm="mount"
uci set fstab.rwm.device="${ORIG_DEVICE}"
uci set fstab.rwm.target="/rwm"

# 5. DATA TRANSFER
echo "5. Creating temporary mount point and copying data..." | tee -a $LOG_FILE
mkdir -p $MOUNT_POINT 2>&1 | tee -a $LOG_FILE
mount $DEVICE $MOUNT_POINT 2>&1 | tee -a $LOG_FILE

if [ $? -ne 0 ]; then
    echo "ERROR! Temporary mounting of $DEVICE failed. Aborting." | tee -a $LOG_FILE
    exit 1
fi

# Copy the contents of the current overlay to the new partition
tar -C /overlay -cvf - . | tar -C $MOUNT_POINT -xf - 2>&1 | tee -a $LOG_FILE

# 6. SWAP FILE CONFIGURATION
echo "6. Creating and configuring the ${SWAP_SIZE_MB}MB SWAP file..." | tee -a $LOG_FILE

# Create the 1GB SWAP file on the SD card (temp mounted at $MOUNT_POINT)
echo "   Creating 1GB file..." | tee -a $LOG_FILE
dd if=/dev/zero of=${MOUNT_POINT}${SWAP_FILE_PATH} bs=1M count=${SWAP_SIZE_MB} 2>&1 | tee -a $LOG_FILE

# Initialize it as SWAP
echo "   Initializing file as SWAP area..." | tee -a $LOG_FILE
mkswap ${MOUNT_POINT}${SWAP_FILE_PATH} 2>&1 | tee -a $LOG_FILE

# Configure the SWAP file in /etc/config/fstab
uci -q delete fstab.swap_file
uci set fstab.swap_file="swap"
uci set fstab.swap_file.device="${TARGET_MOUNT}${SWAP_FILE_PATH}"
uci set fstab.swap_file.enabled='1'

# Save and apply fstab changes
uci commit fstab

# 7. CLEANUP AND REBOOT
echo "7. Cleaning up and unmounting before reboot..." | tee -a $LOG_FILE
sync
umount $MOUNT_POINT
rmdir $MOUNT_POINT

# 8. REBOOT
echo "8. Extroot and SWAP configuration complete. The system will reboot in 5 seconds." | tee -a $LOG_FILE
echo "After reboot, run 'df -h' and 'free' to verify the large storage and SWAP." | tee -a $LOG_FILE
sleep 5
reboot