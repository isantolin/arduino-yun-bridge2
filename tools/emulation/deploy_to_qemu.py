#!/usr/bin/env python3
"""Interactive/Automated deployer for McuBridge inside OpenWrt QEMU VM."""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Annotated

import pexpect
import tenacity
import typer

app = typer.Typer(help="Interactive/Automated deployer for McuBridge inside OpenWrt QEMU VM.", add_completion=False)

PROMPT = r"root@[^:]+:[^#]*#"

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
RELEASE_BASE = f"https://github.com/isantolin/arduino-yun-bridge2/releases/download/v{VERSION}"


def get_pkg_release(pkg_name: str) -> str:
    makefile = REPO_ROOT / "feeds" / pkg_name / "Makefile"
    if not makefile.exists():
        makefile = REPO_ROOT / pkg_name / "Makefile"
    if makefile.exists():
        m = re.search(r"PKG_RELEASE:=(\d+)", makefile.read_text(encoding="utf-8"))
        if m:
            return f"r{m.group(1)}"
    return "r1"


def get_release_apk_names() -> list[str]:
    apks = [
        f"luci-app-mcubridge-{VERSION}-{get_pkg_release('luci-app-mcubridge')}.apk",
        f"mcubridge-{VERSION}-{get_pkg_release('mcubridge')}.apk",
        f"mcubridge-gateway-{VERSION}-{get_pkg_release('mcubridge-gateway')}.apk",
    ]
    manifest_path = REPO_ROOT / "requirements" / "runtime.toml"
    if manifest_path.exists():
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        for dep in data.get("dependency", []):
            openwrt_pkg = dep.get("openwrt", "")
            pip_spec = dep.get("pip", "")
            pkg_dir = REPO_ROOT / "feeds" / openwrt_pkg
            if openwrt_pkg.startswith("python3-") and "==" in pip_spec and pkg_dir.exists():
                _, ver = pip_spec.split("==", 1)
                rel = get_pkg_release(openwrt_pkg)
                apks.append(f"{openwrt_pkg}-{ver}-{rel}.apk")
    return sorted(apks)


APK_NAMES: list[str] = get_release_apk_names()


def run_command_in_console(child: pexpect.spawn[bytes], cmd: str, timeout: int = 60) -> str:
    """Send command and wait for prompt, returning output."""
    sys.stdout.write(f"\n>>> Running: {cmd}\n")
    sys.stdout.flush()
    child.sendline(cmd)
    child.expect(PROMPT, timeout=timeout)
    raw_before: bytes | None = child.before
    output: str = raw_before.decode("utf-8") if raw_before is not None else ""
    sys.stdout.write(output)
    sys.stdout.flush()
    return output


@app.command()
def main(
    domain: Annotated[str, typer.Option("--domain", help="Libvirt VM domain name")] = "openwrt-mcubridge",
    timeout: Annotated[int, typer.Option("--timeout", help="Timeout in seconds for console connection")] = 30,
) -> None:
    print(f"Connecting to {domain} console...")
    child: pexpect.spawn[bytes] = pexpect.spawn(f"virsh -c qemu:///system console --force {domain}", timeout=timeout)
    child.logfile_read = sys.stdout.buffer

    # Send enters to wake up console
    def _wake_console() -> int:
        child.sendline("")
        return child.expect([PROMPT, "Please press Enter to activate this console", pexpect.TIMEOUT], timeout=3)

    wake_retryer = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_fixed(1.0),
        retry=tenacity.retry_if_result(lambda idx: idx not in (0, 1)),
        reraise=False,
    )
    try:
        wake_retryer(_wake_console)
    except tenacity.RetryError as exc:
        print(f"[WARN] Console wake retry exhausted: {exc}")

    child.sendline("")
    child.expect(PROMPT, timeout=15)
    print("\n[OK] Console active and prompt reached.")

    # 1. Bring up eth0 with static IP on libvirt default NAT network
    print("\n[INFO] Bringing up eth0 and network route...")
    run_command_in_console(child, "ip link set eth0 up", timeout=10)
    run_command_in_console(child, "ip addr flush dev eth0", timeout=10)
    run_command_in_console(child, "ip addr add 192.168.122.200/24 dev eth0", timeout=10)
    run_command_in_console(child, "ip route add default via 192.168.122.1 dev eth0 || true", timeout=10)
    run_command_in_console(child, "echo 'nameserver 8.8.8.8' > /tmp/resolv.conf", timeout=10)
    run_command_in_console(child, "ln -sf /tmp/resolv.conf /etc/resolv.conf 2>/dev/null || true", timeout=10)
    run_command_in_console(child, "date -s '2026-08-28 23:30:00'", timeout=10)
    run_command_in_console(child, "ping -c 2 8.8.8.8", timeout=10)

    # 2. Switch repository URLs to http: and update
    print("\n[INFO] Updating OpenWrt package indexes...")
    run_command_in_console(
        child,
        "sed -i 's/https:/http:/g' /etc/apk/repositories /etc/apk/repositories.d/*.list 2>/dev/null || true",
        timeout=10,
    )
    run_command_in_console(child, "apk update", timeout=90)

    # 3. Install SSL support
    print("\n[INFO] Installing SSL packages and wget-ssl...")
    run_command_in_console(child, "apk add ca-bundle ca-certificates libustream-mbedtls wget-ssl", timeout=120)

    # 4. Create deployment directory
    run_command_in_console(child, "mkdir -p /root/deploy/bin", timeout=10)
    run_command_in_console(child, "cd /root/deploy", timeout=10)

    # 5. Download 3_install.sh and APKs from GitHub Release v{VERSION}
    install_script_url = "https://raw.githubusercontent.com/isantolin/arduino-yun-bridge2/main/3_install.sh"
    run_command_in_console(
        child,
        f"wget -c {install_script_url} -O /root/deploy/3_install.sh",
        timeout=30,
    )
    run_command_in_console(child, "chmod +x /root/deploy/3_install.sh", timeout=10)

    print(f"\n[INFO] Downloading APK packages from GitHub Release v{VERSION}...")
    for apk in APK_NAMES:
        dl_cmd = f"wget -c {RELEASE_BASE}/{apk} -O /root/deploy/bin/{apk}"
        run_command_in_console(child, dl_cmd, timeout=60)

    # 6. Execute 3_install.sh with FORCE=1
    print("\n[INFO] Running /root/deploy/3_install.sh with FORCE=1...")
    run_command_in_console(child, "cd /root/deploy && FORCE=1 ./3_install.sh", timeout=600)

    # 7. Check service status
    print("\n[INFO] Verifying daemon status...")
    run_command_in_console(child, "/etc/init.d/mcubridge status", timeout=15)
    run_command_in_console(child, "/etc/init.d/mcubridge-gateway status || true", timeout=15)
    run_command_in_console(child, "ps w | grep -E 'mcubridge|python'", timeout=15)

    print("\n[SUCCESS] McuBridge ecosystem successfully installed inside OpenWrt QEMU VM!")


if __name__ == "__main__":
    app()
