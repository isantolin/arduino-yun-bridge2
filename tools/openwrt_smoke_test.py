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

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Annotated, Any
import urllib.error
import urllib.request

import typer


def log_info(msg: str) -> None:
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()


def log_error(msg: str) -> None:
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENWRT_VERSION = "25.12.5"
TARGET = "malta/be"
BASE_URL = f"https://downloads.openwrt.org/releases/{OPENWRT_VERSION}/targets/{TARGET}"
KERNEL_FILE = f"openwrt-{OPENWRT_VERSION}-malta-be-vmlinux.elf"
ROOTFS_GZ = f"openwrt-{OPENWRT_VERSION}-malta-be-rootfs-ext4.img.gz"
ROOTFS_IMG = "openwrt-rootfs.img"

APK_DISK_MB = 128  # APKs + deploy scripts
EXTROOT_DISK_MB = 2048  # extroot overlay + swap

PROMPT = r"root@.*:.*#"
DEPLOY_SCRIPTS = ["2_expand.sh", "3_install.sh"]

SYS_APKS_BASE_URL = f"https://downloads.openwrt.org/releases/{OPENWRT_VERSION}/packages/mips_24kc/base"
SYS_APKS_TARGET_URL = f"https://downloads.openwrt.org/releases/{OPENWRT_VERSION}/targets/{TARGET}/packages"

SYS_APKS = [
    ("fdisk-2.41.5-r1.apk", SYS_APKS_BASE_URL),
    ("libfdisk1-2.41.5-r1.apk", SYS_APKS_BASE_URL),
    ("libsmartcols1-2.41.5-r1.apk", SYS_APKS_BASE_URL),
    ("libblkid1-2.41.5-r1.apk", SYS_APKS_BASE_URL),
    ("libuuid1-2.41.5-r1.apk", SYS_APKS_BASE_URL),
    ("libncurses6-6.4-r3.apk", SYS_APKS_BASE_URL),
    ("e2fsprogs-1.47.3-r1.apk", SYS_APKS_BASE_URL),
    ("libext2fs2-1.47.3-r1.apk", SYS_APKS_BASE_URL),
    ("libcomerr0-1.47.3-r1.apk", SYS_APKS_BASE_URL),
    ("libss2-1.47.3-r1.apk", SYS_APKS_BASE_URL),
    ("block-mount-2026.05.23~16718b6e-r1.apk", SYS_APKS_TARGET_URL),
    ("blockd-2026.05.23~16718b6e-r1.apk", SYS_APKS_TARGET_URL),
]


def _resolve_package_name(base_url: str, pkg_prefix: str) -> str | None:
    try:
        with urllib.request.urlopen(base_url, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        matches = re.findall(rf'href="({re.escape(pkg_prefix)}[^\"]+\.apk)"', html)
        if matches:
            return sorted(matches)[-1]
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log_error(f"[WARN] Could not list directory {base_url}: {e}")
    return None


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
# System APK download
# ---------------------------------------------------------------------------
def download_system_apks(dest_dir: Path) -> None:
    """Download the required OpenWrt system APKs to a host folder."""
    log_info(f"[INFO] Downloading system APKs to {dest_dir}...")
    dest_dir.mkdir(exist_ok=True, parents=True)

    for filename, base_url in SYS_APKS:
        file_path = dest_dir / filename
        if file_path.exists():
            log_info(f"[INFO] {filename} already exists, skipping download.")
            continue

        url = f"{base_url}/{filename}"
        log_info(f"[INFO] Downloading {url} -> {file_path}")
        try:
            urllib.request.urlretrieve(url, file_path)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log_info(f"[WARN] {filename} returned 404, attempting dynamic resolution from {base_url}...")
                pkg_prefix = filename.split("-202")[0] if "-202" in filename else filename.split("-")[0]
                resolved_name = _resolve_package_name(base_url, pkg_prefix)
                if resolved_name:
                    resolved_url = f"{base_url}/{resolved_name}"
                    resolved_file_path = dest_dir / resolved_name
                    log_info(f"[INFO] Resolved to {resolved_url} -> {resolved_file_path}")
                    urllib.request.urlretrieve(resolved_url, resolved_file_path)
                    continue
            log_error(f"[ERROR] Failed to download {url}: {e}")
            raise
        except urllib.error.URLError as e:
            log_error(f"[ERROR] Failed to download {url}: {e}")
            raise


# ---------------------------------------------------------------------------
# Disk creation
# ---------------------------------------------------------------------------
def create_apk_disk(apk_dir: Path, sys_apk_dir: Path, repo_root: Path) -> str:
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
        log_info(f"[INFO] Copied {len(apk_files)} project APKs to disk bin/.")

        sys_apk_files = list(sys_apk_dir.glob("*.apk"))
        for apk in sys_apk_files:
            run(["sudo", "cp", str(apk), str(bin_dir / apk.name)])
        log_info(f"[INFO] Copied {len(sys_apk_files)} system APKs to disk bin/.")

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
    run(
        [
            "dd",
            "if=/dev/zero",
            f"of={extroot_disk}",
            "bs=1M",
            f"count={EXTROOT_DISK_MB}",
        ]
    )
    return extroot_disk


# ---------------------------------------------------------------------------
# QEMU helpers
# ---------------------------------------------------------------------------
def build_qemu_cmd(apk_disk: str, extroot_disk: str) -> list[str]:
    return [
        "qemu-system-mips",
        "-M",
        "malta",
        "-kernel",
        KERNEL_FILE,
        "-drive",
        f"file={ROOTFS_IMG},format=raw,if=ide",  # sda — rootfs
        "-drive",
        f"file={apk_disk},format=raw,if=ide",  # sdb — APKs + scripts
        "-drive",
        f"file={extroot_disk},format=raw,if=ide",  # sdc — extroot target
        "-append",
        "root=/dev/sda console=ttyS0",
        "-nographic",
        "-serial",
        "mon:stdio",
        "-m",
        "256",
        # [SIL-2] Improved QEMU networking for Malta (pcnet is more native than virtio)
        "-netdev",
        "user,id=net0,ipv6=off",
        "-device",
        "pcnet,netdev=net0",
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
    wait_for_prompt(child, timeout=60)


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
    # [SIL-2] Keep /mnt mounted during expansion so script can use local APKs
    # send_and_wait(child, "umount /mnt", timeout=5)

    # Pre-set UCI to skip interactive confirmation and enable internet
    send_and_wait(
        child,
        "touch /etc/config/mcubridge; "
        "uci set mcubridge.general=settings 2>/dev/null; "
        "uci set mcubridge.general.extroot_force=1 2>/dev/null; "
        "uci commit mcubridge 2>/dev/null || true",
        timeout=10,
    )

    # Bring up network for apk update via DHCP on LAN bridge
    send_and_wait(
        child,
        "sysctl -w net.ipv6.conf.all.disable_ipv6=1 || true; "
        "sysctl -w net.ipv6.conf.default.disable_ipv6=1 || true; "
        "uci set network.lan.proto='dhcp'; "
        "uci commit network; "
        "/etc/init.d/network restart",
        timeout=30,
    )

    # [SIL-2] Strong Network Fix: DNS + Time + Force IPv4 for wget
    # Avoid overwriting nameserver 8.8.8.8 in QEMU SLIRP as it breaks DNS forwarding via gateway (10.0.2.3)
    send_and_wait(child, "date -s '2026-01-01 12:00:00'", timeout=5)
    send_and_wait(child, "echo 'alias wget=\"wget -4\"' >> /etc/profile", timeout=5)
    send_and_wait(
        child,
        "sed -i 's/https:/http:/g' /etc/apk/repositories /etc/apk/repositories.d/*.list 2>/dev/null || true",
        timeout=5,
    )
    send_and_wait(child, "ping -c 2 8.8.8.8 || true", timeout=15)

    # Wait for network to establish
    child.sendline("sleep 10")
    wait_for_prompt(child, timeout=15)

    # Run with 512MB swap and target /dev/vdc
    # The script ends with sleep 5 + reboot
    # Use FORCE=1 to ensure non-interactive execution
    child.sendline("FORCE=1 /root/2_expand.sh 512 /dev/sdc")

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

    # Bring up network for apk update via DHCP on LAN bridge
    send_and_wait(
        child,
        "sysctl -w net.ipv6.conf.all.disable_ipv6=1 || true; "
        "sysctl -w net.ipv6.conf.default.disable_ipv6=1 || true; "
        "uci set network.lan.proto='dhcp'; "
        "uci commit network; "
        "/etc/init.d/network restart",
        timeout=30,
    )

    # [SIL-2] Strong Network Fix: DNS + Time + Force IPv4 for wget
    # Avoid overwriting nameserver 8.8.8.8 in QEMU SLIRP as it breaks DNS forwarding via gateway (10.0.2.3)
    send_and_wait(child, "date -s '2026-01-01 12:00:00'", timeout=5)
    send_and_wait(child, "echo 'alias wget=\"wget -4\"' >> /etc/profile", timeout=5)
    send_and_wait(
        child,
        "sed -i 's/https:/http:/g' /etc/apk/repositories /etc/apk/repositories.d/*.list 2>/dev/null || true",
        timeout=5,
    )
    send_and_wait(child, "ping -c 2 8.8.8.8 || true", timeout=15)

    # Wait for network to establish
    child.sendline("sleep 10")
    wait_for_prompt(child, timeout=15)

    # Run non-interactively: pipe "n" for the PPP removal prompt
    child.sendline("cd /root/deploy && echo 'n' | FORCE=1 sh ./3_install.sh")

    # Wait for the final success message (long timeout for package installs)
    child.expect("Installation Complete", timeout=600)
    wait_for_prompt(child, timeout=30)
    log_info("[PHASE 2] 3_install.sh completed successfully.")


def phase_verify(child: Any) -> None:
    """Phase 3: Verify mcubridge installation and daemon."""
    log_info("[PHASE 3] Verifying installation...")

    # 1. Init script exists and is executable
    child.sendline("test -x /etc/init.d/mcubridge && echo 'VERIFY_INIT_OK' || echo 'VERIFY_INIT_FAIL'")
    child.expect(r"VERIFY_INIT_(OK|FAIL)", timeout=10)
    if "VERIFY_INIT_FAIL" in child.after:
        raise ValueError("Verification failed: /etc/init.d/mcubridge not found or not executable.")
    wait_for_prompt(child, timeout=10)

    # 2. Cloud gateway exists and is executable
    child.sendline("test -x /usr/bin/mcubridge-gateway && echo 'VERIFY_GW_OK' || echo 'VERIFY_GW_FAIL'")
    child.expect(r"VERIFY_GW_(OK|FAIL)", timeout=10)
    if "VERIFY_GW_FAIL" in child.after:
        raise ValueError("Verification failed: /usr/bin/mcubridge-gateway not found or not executable.")
    wait_for_prompt(child, timeout=10)

    # 3. UCI configuration was created
    send_and_wait(child, "uci show mcubridge 2>/dev/null | head -10", timeout=10)

    # 4. Serial secret was generated (valid 64-character hex key)
    child.sendline("uci -q get mcubridge.general.serial_shared_secret")
    wait_for_prompt(child, timeout=10)
    output = getattr(child, "before", "")
    valid_secret = any(
        len(line.strip()) == 64 and all(c in "0123456789abcdefABCDEF" for c in line.strip())
        for line in output.splitlines()
    )
    if not valid_secret:
        raise ValueError(f"Verification failed: invalid serial_shared_secret in UCI. Output:\n{output}")

    # 5. Service is enabled in rc.d
    child.sendline("ls -l /etc/rc.d/*mcubridge* && echo 'VERIFY_RCD_OK' || echo 'VERIFY_RCD_FAIL'")
    child.expect(r"VERIFY_RCD_(OK|FAIL)", timeout=10)
    if "VERIFY_RCD_FAIL" in child.after:
        raise ValueError("Verification failed: mcubridge service not enabled in /etc/rc.d/.")
    wait_for_prompt(child, timeout=10)

    # 6. Core mcubridge packages installed in APK database
    child.sendline(
        "apk info -e mcubridge && apk info -e mcubridge-gateway && echo 'VERIFY_APK_OK' || echo 'VERIFY_APK_FAIL'"
    )
    child.expect(r"VERIFY_APK_(OK|FAIL)", timeout=10)
    if "VERIFY_APK_FAIL" in child.after:
        raise ValueError("Verification failed: core mcubridge packages not installed in apk database.")
    wait_for_prompt(child, timeout=10)

    log_info("[SUCCESS] Full pipeline smoke test passed!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_test(apk_disk: str, extroot_disk: str) -> None:
    log_info("[INFO] Starting QEMU Emulation (full pipeline)...")

    pexpect = __import__("pexpect")

    qemu_cmd = build_qemu_cmd(apk_disk, extroot_disk)
    child: Any = pexpect.spawn(qemu_cmd[0], qemu_cmd[1:], encoding="utf-8", timeout=300)
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

    except (pexpect.ExceptionPexpect, OSError, ValueError) as e:
        log_error(f"\n[ERROR] Test failed: {e}")
        if hasattr(child, "before") and child.before:
            log_error(f"[DEBUG] Last output:\n{child.before[-500:]}")
        child.terminate(force=True)
        sys.exit(1)


app = typer.Typer(help="OpenWrt Smoke Test using QEMU (MIPS Malta).", add_completion=False)


@app.command()
def main(
    apk_directory: Annotated[Path, typer.Argument(help="Path to directory containing APK packages")],
) -> None:
    repo_root = Path(__file__).resolve().parent.parent

    download_images()
    sys_apk_dir = repo_root / "dl_sys_apks"
    download_system_apks(sys_apk_dir)

    apk_disk = create_apk_disk(apk_directory, sys_apk_dir, repo_root)
    extroot_disk = create_extroot_disk()
    run_test(apk_disk, extroot_disk)


if __name__ == "__main__":
    app()
