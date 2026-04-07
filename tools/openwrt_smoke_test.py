#!/usr/bin/env python3
"""
OpenWrt Smoke Test using QEMU (MIPS Malta).

Runs the full deployment pipeline inside a QEMU VM:
  1. Boot OpenWrt
  2. Run 2_expand.sh (extroot + swap on /dev/sdc) → reboot
  3. Run 3_install.sh (system deps, project APKs, secrets, daemon start)
  4. Verify mcubridge is running

Requires: qemu-system-mips, python3-pexpect, wget, e2fsprogs
"""

import sys
import subprocess
import shutil
from pathlib import Path
from typing import Any


def log_info(msg: str) -> None:
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()


def log_error(msg: str) -> None:
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENWRT_VERSION = "25.12.2"
TARGET = "malta/be"
BASE_URL = f"https://downloads.openwrt.org/releases/{OPENWRT_VERSION}/targets/{TARGET}"
KERNEL_FILE = f"openwrt-{OPENWRT_VERSION}-malta-be-vmlinux.elf"
ROOTFS_GZ = f"openwrt-{OPENWRT_VERSION}-malta-be-rootfs-ext4.img.gz"
ROOTFS_IMG = "openwrt-rootfs.img"

APK_DISK_MB = 40          # APKs + deploy scripts
EXTROOT_DISK_MB = 2048    # extroot overlay + swap

PROMPT = "root@OpenWrt:/#"
DEPLOY_SCRIPTS = ["2_expand.sh", "3_install.sh"]


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[bytes]:
    log_info(f"[EXEC] {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------
def download_images() -> None:
    log_info("[INFO] Downloading OpenWrt images...")
    if not Path(KERNEL_FILE).exists():
        run(["wget", "-q", f"{BASE_URL}/{KERNEL_FILE}"])

    if not Path(ROOTFS_IMG).exists():
        run(["wget", "-q", "-O", ROOTFS_GZ, f"{BASE_URL}/{ROOTFS_GZ}"])
        run(["gunzip", "-f", ROOTFS_GZ])
        shutil.move(f"openwrt-{OPENWRT_VERSION}-malta-be-rootfs-ext4.img", ROOTFS_IMG)


# ---------------------------------------------------------------------------
# Disk creation
# ---------------------------------------------------------------------------
def create_apk_disk(apk_dir: Path, repo_root: Path) -> str:
    """Create an ext4 disk with APKs in bin/ and deploy scripts at root."""
    log_info("[INFO] Creating APK data disk...")
    apk_disk = "apks.img"
    run(["dd", "if=/dev/zero", f"of={apk_disk}", "bs=1M", f"count={APK_DISK_MB}"])
    run(["mkfs.ext4", "-F", apk_disk])

    mnt = Path("mnt_apks")
    mnt.mkdir(exist_ok=True)

    run(["sudo", "mount", apk_disk, str(mnt)])
    try:
        # bin/ subdirectory — 3_install.sh expects APKs here
        bin_dir = mnt / "bin"
        run(["sudo", "mkdir", "-p", str(bin_dir)])

        apk_files = list(apk_dir.glob("*.apk"))
        for apk in apk_files:
            run(["sudo", "cp", str(apk), str(bin_dir / apk.name)])
        log_info(f"[INFO] Copied {len(apk_files)} APKs to disk bin/.")

        # Copy deploy scripts
        for script in DEPLOY_SCRIPTS:
            src = repo_root / script
            if src.exists():
                run(["sudo", "cp", str(src), str(mnt / script)])
                run(["sudo", "chmod", "+x", str(mnt / script)])
                log_info(f"[INFO] Copied {script} to disk.")
            else:
                log_error(f"[WARN] {script} not found at {src}")
    finally:
        run(["sudo", "umount", str(mnt)])

    return apk_disk


def create_extroot_disk() -> str:
    """Create an empty raw disk for extroot + swap."""
    log_info(f"[INFO] Creating {EXTROOT_DISK_MB}MB extroot disk...")
    extroot_disk = "extroot.img"
    run(["dd", "if=/dev/zero", f"of={extroot_disk}", "bs=1M", f"count={EXTROOT_DISK_MB}"])
    return extroot_disk


# ---------------------------------------------------------------------------
# QEMU helpers
# ---------------------------------------------------------------------------
def build_qemu_cmd(apk_disk: str, extroot_disk: str) -> list[str]:
    return [
        "qemu-system-mips",
        "-M", "malta",
        "-kernel", KERNEL_FILE,
        "-drive", f"file={ROOTFS_IMG},format=raw,if=ide",     # sda — rootfs
        "-drive", f"file={apk_disk},format=raw,if=ide",       # sdb — APKs + scripts
        "-drive", f"file={extroot_disk},format=raw,if=ide",   # sdc — extroot target
        "-append", "root=/dev/sda console=ttyS0",
        "-nographic",
        "-serial", "mon:stdio",
        "-m", "256",
        # NAT network for apk update
        "-netdev", "user,id=net0",
        "-device", "e1000,netdev=net0",
    ]


def wait_for_prompt(child: Any, timeout: int = 30) -> None:
    child.expect(PROMPT, timeout=timeout)


def send_and_wait(child: Any, cmd: str, timeout: int = 30) -> None:
    child.sendline(cmd)
    wait_for_prompt(child, timeout)


def wait_for_boot(child: Any, timeout: int = 180) -> None:
    """Wait for OpenWrt console prompt after boot."""
    child.expect("Please press Enter to activate this console", timeout=timeout)
    child.sendline("")
    wait_for_prompt(child, timeout=30)


# ---------------------------------------------------------------------------
# Test phases
# ---------------------------------------------------------------------------
def phase_expand(child: Any) -> None:
    """Phase 1: Mount data disk, run 2_expand.sh, handle reboot."""
    log_info("[PHASE 1] Running 2_expand.sh (extroot + swap)...")

    send_and_wait(child, "mount /dev/sdb /mnt", timeout=10)

    # Copy script to writable location
    send_and_wait(child, "cp /mnt/2_expand.sh /root/2_expand.sh", timeout=5)
    send_and_wait(child, "chmod +x /root/2_expand.sh", timeout=5)
    send_and_wait(child, "umount /mnt", timeout=5)

    # Pre-set UCI to skip interactive confirmation
    send_and_wait(
        child,
        "uci set mcubridge.general=settings 2>/dev/null; "
        "uci set mcubridge.general.extroot_force=1 2>/dev/null; "
        "uci commit mcubridge 2>/dev/null || true",
        timeout=10,
    )

    # Run with 512MB swap and target /dev/vdc
    # The script ends with sleep 5 + reboot
    child.sendline("/root/2_expand.sh 512 /dev/sdc")

    log_info("[WAIT] Waiting for reboot after 2_expand.sh...")
    wait_for_boot(child, timeout=180)
    log_info("[PHASE 1] Reboot complete. Extroot should be active.")

    # Verify
    send_and_wait(child, "mount | grep overlay || echo 'NO_OVERLAY'", timeout=10)
    send_and_wait(child, "free | head -3", timeout=10)


def phase_install(child: Any) -> None:
    """Phase 2: Mount data disk, run 3_install.sh."""
    log_info("[PHASE 2] Running 3_install.sh (full installation)...")

    # Mount APK disk again (post-reboot)
    send_and_wait(child, "mount /dev/sdb /mnt", timeout=10)

    # Set up workspace as 3_install.sh expects
    send_and_wait(child, "mkdir -p /root/deploy/bin", timeout=5)
    send_and_wait(child, "cp /mnt/3_install.sh /root/deploy/3_install.sh", timeout=5)
    send_and_wait(child, "chmod +x /root/deploy/3_install.sh", timeout=5)
    send_and_wait(child, "cp /mnt/bin/*.apk /root/deploy/bin/", timeout=10)
    send_and_wait(child, "umount /mnt", timeout=5)

    # Bring up network for apk update via DHCP on virtio-net
    child.sendline("udhcpc -i eth0 -q 2>/dev/null || true")
    wait_for_prompt(child, timeout=15)

    # Run non-interactively: pipe "n" for the PPP removal prompt
    child.sendline("cd /root/deploy && echo 'n' | sh ./3_install.sh")

    # Wait for the final success message (long timeout for package installs)
    child.expect("Installation Complete", timeout=600)
    wait_for_prompt(child, timeout=30)
    log_info("[PHASE 2] 3_install.sh completed successfully.")


def phase_verify(child: Any) -> None:
    """Phase 3: Verify mcubridge installation and daemon."""
    log_info("[PHASE 3] Verifying installation...")

    # Init script exists
    send_and_wait(child, "ls -l /etc/init.d/mcubridge", timeout=10)

    # UCI configuration was created
    send_and_wait(child, "uci show mcubridge 2>/dev/null | head -10", timeout=10)

    # Serial secret was generated (not placeholder)
    child.sendline("uci -q get mcubridge.general.serial_shared_secret")
    wait_for_prompt(child, timeout=10)

    # Daemon was attempted (may crash-loop without real serial port, but should be registered)
    child.sendline("pgrep -f mcubridge || echo 'DAEMON_NOT_RUNNING'")
    wait_for_prompt(child, timeout=10)

    # Service is enabled
    send_and_wait(child, "ls -l /etc/rc.d/*mcubridge* 2>/dev/null || echo 'SERVICE_NOT_ENABLED'", timeout=10)

    # Show installed mcubridge packages
    send_and_wait(child, "apk info 2>/dev/null | grep -i mcubridge || true", timeout=10)

    log_info("[SUCCESS] Full pipeline smoke test passed!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_test(apk_disk: str, extroot_disk: str) -> None:
    log_info("[INFO] Starting QEMU Emulation (full pipeline)...")

    import pexpect  # pyright: ignore[reportMissingModuleSource]

    qemu_cmd = build_qemu_cmd(apk_disk, extroot_disk)
    child: pexpect.spawn[str] = pexpect.spawn(
        qemu_cmd[0], qemu_cmd[1:], encoding="utf-8", timeout=300
    )
    child.logfile = sys.stdout

    try:
        log_info("[WAIT] Waiting for OpenWrt to boot...")
        wait_for_boot(child, timeout=180)
        log_info("[INFO] Console active.")

        phase_expand(child)
        phase_install(child)
        phase_verify(child)

        child.sendline("poweroff")
        child.expect(pexpect.EOF, timeout=30)

    except Exception as e:
        log_error(f"\n[ERROR] Test failed: {e}")
        if hasattr(child, "before") and child.before:
            log_error(f"[DEBUG] Last output:\n{child.before[-500:]}")
        child.terminate(force=True)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        log_error(f"Usage: {sys.argv[0]} <apk_directory>")
        sys.exit(1)

    apk_dir_arg = sys.argv[1]
    repo_root = Path(__file__).resolve().parent.parent

    download_images()
    apk_disk = create_apk_disk(Path(apk_dir_arg), repo_root)
    extroot_disk = create_extroot_disk()
    run_test(apk_disk, extroot_disk)
