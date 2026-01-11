#!/bin/sh

# -----------------------------------------------------------------------------
# Script: 2_expand.sh
# Description: Configures Extroot (overlay) and SWAP on an external storage device.
#              Expanded to support QEMU/Virtual environments with raw disks.
# Usage: ./2_expand.sh [swap_size_mb] [device]
# -----------------------------------------------------------------------------

SWAP_SIZE_MB=1024
DEVICE=""

# --- Helper Functions ---

log_info() {
    echo "[INFO] $1"
}

log_warn() {
    echo "[WARN] $1"
}

log_err() {
    echo "[ERROR] $1"
}

# --- Argument Parsing ---

if [ -n "$1" ]; then
    SWAP_SIZE_MB=$1
fi

if [ -n "$2" ]; then
    DEVICE=$2
fi

echo ""
echo "--- Starting Extroot and SWAP Script (Robust Mode) ---"

# --- 0. Device Identification & Auto-Partitioning ---

if [ -z "$DEVICE" ]; then
    echo "Attempting to find SD card device..."
    # Try to find a suitable candidate (sda1, sdb1, mmcblk0p1)
    CANDIDATE=$(grep -E 'sd[a-z]1|mmcblk[0-9]p1' /proc/partitions | sort -k 3 -n -r | head -n 1 | awk '{print $4}')
    
    if [ -n "$CANDIDATE" ]; then
        DEVICE="/dev/$CANDIDATE"
    else
        # Try finding raw devices (sdb) common in VMs
        RAW_CANDIDATE=$(grep -E 'sd[a-z]$' /proc/partitions | grep -v 'sda' | head -n 1 | awk '{print $4}')
        if [ -n "$RAW_CANDIDATE" ]; then
             DEVICE="/dev/$RAW_CANDIDATE"
             log_info "Identified raw device in VM: $DEVICE"
        fi
    fi
fi

if [ -z "$DEVICE" ]; then
    log_err "No suitable storage device found. Please specify manually."
    echo "Usage: $0 <swap_mb> <device>"
    exit 1
fi

log_info "Target device: $DEVICE"

# Check if device is a raw disk (no number at the end) and partition it if necessary
case "$DEVICE" in
    *[!0-9])
        log_warn "Device $DEVICE appears to be a raw disk (no partition detected)."
        echo "QEMU/VM Environment detected. Attempting to partition $DEVICE..."
        
        # Check if fdisk is available
        if ! command -v fdisk >/dev/null; then
            echo "Installing partitioning tools..."
            apk update && apk add fdisk
        fi
        
        echo "Creating partition table on $DEVICE..."
        # Create new DOS label, Primary partition 1, Use all space
        printf "o\nn\np\n1\n\n\nw\n" | fdisk "$DEVICE"
        
        # Refresh device list
        sync
        sleep 2
        
        # Update DEVICE target to the new partition
        if [ -e "${DEVICE}1" ]; then
            DEVICE="${DEVICE}1"
            log_info "Successfully partitioned. New target: $DEVICE"
        else
            log_err "Partitioning failed or ${DEVICE}1 not found."
            exit 1
        fi
        ;;
esac

# --- 1. Dependencies ---

echo "1. Checking and installing required packages..."
if apk info | grep -q "e2fsprogs"; then
    echo "   [OK] Required packages (e2fsprogs) are already installed."
else
    echo "   Installing e2fsprogs, block-mount, fdisk..."
    apk update
    apk add e2fsprogs block-mount fdisk
fi

# --- 2. Extroot Configuration ---

echo "2. Verifying Extroot configuration..."

# Check if extroot is already active on this device
CURRENT_OVERLAY=$(mount | grep "on /overlay type" | awk '{print $1}')
TARGET_UUID=$(block info "$DEVICE" | grep -o 'UUID="[^"]*"' | sed 's/UUID="//;s/"//')

if [ -n "$TARGET_UUID" ] && block info | grep "/overlay" | grep -q "$TARGET_UUID"; then
    echo "   [OK] Extroot is already active on $DEVICE."
    SKIP_EXTROOT=1
else
    echo "   [FAIL] Extroot is not active on $DEVICE."
    SKIP_EXTROOT=0
fi

if [ "$SKIP_EXTROOT" -eq 0 ]; then
    echo "2.1 Extroot requires configuration. Proceeding to format and configure..."
    
    # Confirmation skip check via UCI (optional)
    FORCE=$(uci -q get mcubridge.general.extroot_force)
    if [ "$FORCE" != "1" ]; then
        read -p "CONFIRM: $DEVICE will be formatted and configured as overlay. Continue? [y/N]: " CONFIRM
        if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
            echo "   Aborted by user."
            exit 0
        fi
    fi
    echo "   >> Proceeding with formatting."

    echo "   2.2 Unmounting and formatting $DEVICE to ext4..."
    # Robust unmount
    mount | grep "$DEVICE" | awk '{print $3}' | xargs -r umount -f >/dev/null 2>&1
    
    # Format
    mkfs.ext4 -F -L extroot "$DEVICE" >/dev/null
    
    echo "   2.3 Configuring /etc/config/fstab for the new overlay..."
    eval $(block info "$DEVICE" | grep -o -e "UUID=\S*")
    echo "   Extracted UUID: '$UUID'"
    
    if [ -z "$UUID" ]; then
        log_err "Failed to get UUID for $DEVICE"
        exit 1
    fi

    # Configure fstab via UCI
    uci -q delete fstab.overlay
    uci set fstab.overlay="mount"
    uci set fstab.overlay.uuid="$UUID"
    uci set fstab.overlay.target="/overlay"
    uci set fstab.overlay.enabled='1'
    uci commit fstab
    
    echo "   fstab update with uci finished."

    echo "   2.4 Handling overlay fallback..."
    # In QEMU/VM with direct rootfs mount, /overlay might not exist or be a tmpfs.
    # We skip copying data if we are already on a writable rootfs to avoid circular copies.
    
    ROOT_DEV=$(mount | grep "on / type" | awk '{print $1}')
    if [ "$ROOT_DEV" = "/dev/root" ] || [ "$ROOT_DEV" = "/dev/sda" ]; then
        echo "   [INFO] Detected direct rootfs mount ($ROOT_DEV). Skipping data copy to avoid duplication."
    else
        echo "   2.5 Creating temporary mount point and copying data..."
        mkdir -p /mnt/new_overlay
        mount "$DEVICE" /mnt/new_overlay
        # Copy current overlay data to new device
        if [ -d /overlay ]; then
            cp -a -f /overlay/. /mnt/new_overlay
        fi
        umount /mnt/new_overlay
        rmdir /mnt/new_overlay
    fi
fi

# --- 3. SWAP Configuration ---

echo "3. Verifying SWAP configuration..."
# Check if swap is active
if free | grep -q "Swap:.*[1-9]"; then
    echo "   [OK] SWAP is already active."
else
    echo "   [FAIL] SWAP is not active."
    echo "3.1 SWAP requires configuration. Proceeding to create the ${SWAP_SIZE_MB}MB file..."
    
    # Mount temporarily to create swap file
    mkdir -p /mnt/swap_temp
    mount "$DEVICE" /mnt/swap_temp
    
    # --- Space Check (Added) ---
    DF_OUT=$(df -k /mnt/swap_temp | tail -n 1)
    AVAIL_KB=$(echo "$DF_OUT" | awk '{print $4}')
    REQ_KB=$(($SWAP_SIZE_MB * 1024))
    
    echo "   Available space: $((AVAIL_KB/1024))MB. Required: ${SWAP_SIZE_MB}MB."
    
    if [ "$AVAIL_KB" -lt "$REQ_KB" ]; then
        log_err "Insufficient space on device for swap file."
        log_warn "Please use a larger disk (e.g. 2GB) or reduce swap size."
        umount /mnt/swap_temp
        rmdir /mnt/swap_temp
        exit 1
    fi
    # ---------------------------
    
    if [ -f /mnt/swap_temp/swapfile ]; then
        echo "   Swapfile already exists, re-using."
    else
        echo "   Creating and configuring the SWAP file (this may take a moment)..."
        dd if=/dev/zero of=/mnt/swap_temp/swapfile bs=1M count="$SWAP_SIZE_MB"
        mkswap /mnt/swap_temp/swapfile
    fi
    
    # Configure fstab for swap
    uci -q delete fstab.swap
    uci set fstab.swap="swap"
    uci set fstab.swap.device="/overlay/swapfile"
    uci set fstab.swap.enabled='1'
    uci commit fstab
    
    umount /mnt/swap_temp
    rmdir /mnt/swap_temp
    
    echo "   SWAP configured. It will be enabled on next boot."
fi

# --- 4. Service Enablement ---

echo "4. Ensuring fstab init script is enabled for future boots..."
/etc/init.d/fstab enable
/etc/init.d/fstab start

echo "5. Configuration saved. System will reboot in 5 seconds."
echo "   After reboot, run 'df -h' and 'free' to verify."
sleep 5
reboot
