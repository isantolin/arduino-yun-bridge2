#!/usr/bin/env python3
"""Interactive/Automated deployer for McuBridge inside OpenWrt QEMU VM."""

from __future__ import annotations

import sys
import time
import pexpect

PROMPT = r"root@[^:]+:[^#]*#"

RELEASE_BASE = "https://github.com/isantolin/arduino-yun-bridge2/releases/download/v2.8.6"
APK_NAMES: list[str] = [
    "luci-app-mcubridge-2.8.6-r1.apk",
    "mcubridge-2.8.6-r1.apk",
    "mcubridge-gateway-2.8.6-r1.apk",
    "python3-annotated-doc-0.0.5-r1.apk",
    "python3-cobs-1.2.2-r1.apk",
    "python3-cryptography-50.0.1-r1.apk",
    "python3-grpclib-0.4.9-r1.apk",
    "python3-h2-4.4.1-r1.apk",
    "python3-hpack-4.2.0-r1.apk",
    "python3-hyperframe-6.1.0-r1.apk",
    "python3-lmdb-2.3.0-r1.apk",
    "python3-packaging-26.3-r1.apk",
    "python3-packaging-src-25.0-r1.apk",
    "python3-prometheus-client-0.26.0-r1.apk",
    "python3-protobuf-7.36.0-r1.apk",
    "python3-serialx-1.9.0-r1.apk",
    "python3-shellingham-1.5.4-r1.apk",
    "python3-structlog-26.1.0-r1.apk",
    "python3-tenacity-9.1.4-r1.apk",
    "python3-typer-0.27.1-r1.apk",
    "python3-uvloop-0.22.1-r3.apk",
]


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


def main() -> None:
    print("Connecting to openwrt-mcubridge console...")
    child: pexpect.spawn[bytes] = pexpect.spawn("virsh -c qemu:///system console --force openwrt-mcubridge", timeout=30)
    child.logfile_read = sys.stdout.buffer

    # Send enters to wake up console
    for _ in range(5):
        child.sendline("")
        time.sleep(1)
        index = child.expect([PROMPT, "Please press Enter to activate this console", pexpect.TIMEOUT], timeout=3)
        if index in (0, 1):
            break

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

    # 5. Download 3_install.sh and APKs from GitHub Release v2.8.6
    install_script_url = "https://raw.githubusercontent.com/isantolin/arduino-yun-bridge2/main/3_install.sh"
    run_command_in_console(
        child,
        f"wget -c {install_script_url} -O /root/deploy/3_install.sh",
        timeout=30,
    )
    run_command_in_console(child, "chmod +x /root/deploy/3_install.sh", timeout=10)

    print("\n[INFO] Downloading APK packages from GitHub Release v2.8.6...")
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
    main()
