#!/usr/bin/env python3
"""
OpenWrt Smoke Test using QEMU (MIPS Malta).
Verifies that the generated APK packages can be installed and the service starts.
"""

import sys
import subprocess
import shutil
from pathlib import Path

# Configuration based on tools/qemu-mcubridge.xml and 1_compile.sh
OPENWRT_VERSION = "25.12.0"
TARGET = "malta/be"
BASE_URL = f"https://downloads.openwrt.org/releases/{OPENWRT_VERSION}/targets/{TARGET}"
KERNEL_FILE = f"openwrt-{OPENWRT_VERSION}-malta-be-vmlinux.elf"
ROOTFS_GZ = f"openwrt-{OPENWRT_VERSION}-malta-be-rootfs-ext4.img.gz"
ROOTFS_IMG = "openwrt-rootfs.img"

def run(cmd, check=True):
    print(f"[EXEC] {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)

def download_images():
    print("[INFO] Downloading OpenWrt images...")
    if not Path(KERNEL_FILE).exists():
        run(["wget", "-q", f"{BASE_URL}/{KERNEL_FILE}"])

    if not Path(ROOTFS_IMG).exists():
        run(["wget", "-q", "-O", f"{ROOTFS_GZ}", f"{BASE_URL}/{ROOTFS_GZ}"])
        run(["gunzip", "-f", ROOTFS_GZ])
        shutil.move(f"openwrt-{OPENWRT_VERSION}-malta-be-rootfs-ext4.img", ROOTFS_IMG)

def create_apk_disk(apk_dir):
    print("[INFO] Creating APK data disk...")
    apk_disk = "apks.img"
    # Create a 20MB ext4 disk
    run(["dd", "if=/dev/zero", f"of={apk_disk}", "bs=1M", "count=20"])
    run(["mkfs.ext4", "-F", apk_disk])

    # Use a temporary mount point
    mnt = Path("mnt_apks")
    mnt.mkdir(exist_ok=True)

    # Copy APKs using debugfs or mount if sudo is available
    # In CI we have sudo
    run(["sudo", "mount", apk_disk, str(mnt)])
    try:
        for apk in Path(apk_dir).glob("*.apk"):
            shutil.copy(apk, mnt)
        print(f"[INFO] Copied {len(list(Path(apk_dir).glob('*.apk')))} APKs to disk.")
    finally:
        run(["sudo", "umount", str(mnt)])

    return apk_disk

def run_test(apk_disk):
    print("[INFO] Starting QEMU Emulation...")
    # Based on the XML but adapted for CLI/CI
    qemu_cmd = [
        "qemu-system-mips",
        "-M", "malta",
        "-kernel", KERNEL_FILE,
        "-drive", f"file={ROOTFS_IMG},format=raw,if=virtio",
        "-drive", f"file={apk_disk},format=raw,if=virtio",
        "-append", "root=/dev/vda console=ttyS0",
        "-nographic",
        "-serial", "mon:stdio",
        "-m", "256"
    ]

    import pexpect

    # Increase timeout for slow MIPS emulation
    child = pexpect.spawn(qemu_cmd[0], qemu_cmd[1:], encoding='utf-8', timeout=300)
    child.logfile = sys.stdout

    try:
        print("[WAIT] Waiting for OpenWrt to boot...")
        child.expect("Please press Enter to activate this console", timeout=120)
        child.sendline("")

        child.expect("root@OpenWrt:/#", timeout=30)
        print("[INFO] Console active. Mounting APK disk...")
        child.sendline("mount /dev/vdb /mnt")

        child.expect("root@OpenWrt:/#", timeout=10)
        print("[INFO] Installing APKs...")
        # Install all APKs found in /mnt
        # Use --allow-untrusted because they are locally built
        child.sendline("apk add --allow-untrusted /mnt/*.apk")

        # This might take a while as it processes multiple packages
        child.expect("OK", timeout=120)
        child.expect("root@OpenWrt:/#", timeout=10)

        print("[INFO] Verifying McuBridge installation...")
        child.sendline("ls -l /etc/init.d/mcubridge")
        child.expect("/etc/init.d/mcubridge")

        print("[INFO] Starting McuBridge service...")
        child.sendline("/etc/init.d/mcubridge enable")
        child.expect("root@OpenWrt:/#")
        child.sendline("/etc/init.d/mcubridge start")
        child.expect("root@OpenWrt:/#")

        print("[INFO] Checking if process is running...")
        child.sendline("pgrep -f mcubridge")
        # pgrep returns the PID if found
        child.expect(r"\d+")

        print("[SUCCESS] Smoke test passed!")
        child.sendline("poweroff")
        child.expect(pexpect.EOF)

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        child.terminate(force=True)
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <apk_directory>")
        sys.exit(1)

    apk_dir = sys.argv[1]
    download_images()
    apk_disk = create_apk_disk(apk_dir)
    run_test(apk_disk)
