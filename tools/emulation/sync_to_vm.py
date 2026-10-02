#!/usr/bin/env python3
"""Synchronize repository files into the OpenWrt QEMU VM.

Copies the active source code, scripts, configuration, LuCI assets, and
client examples from the host repository directly into the running OpenWrt VM.
"""

from __future__ import annotations

import asyncio
import fnmatch
import io
import socket
import sys
import tarfile
from pathlib import Path
from typing import Annotated

import asyncssh
import typer

REPO_ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(help="Synchronize local McuBridge source files into OpenWrt VM.", add_completion=False)


def _is_ssh_reachable(host: str, port: int = 22, timeout: float = 1.0) -> bool:
    """Test TCP connectivity to SSH port via standard library socket."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_remote_ssh(
    cmd: str,
    host: str,
    user: str = "root",
    timeout: float = 30.0,
    check: bool = True,
) -> tuple[int, str, str]:
    """Execute a shell command on remote host directly via asyncssh."""
    sys.stdout.write(f"[*] Remote SSH ({user}@{host}): {cmd}\n")
    sys.stdout.flush()

    async def _run() -> tuple[int, str, str]:
        async with asyncssh.connect(host, username=user, known_hosts=None) as conn:
            res = await asyncio.wait_for(conn.run(cmd), timeout=timeout)
            return res.exit_status or 0, str(res.stdout or ""), str(res.stderr or "")

    code, out, err = asyncio.run(_run())
    if check and code != 0:
        raise RuntimeError(f"Remote command failed on {host} (exit {code}): {err}")
    return code, out, err


def tar_push(src_dir: Path, remote_dest: str, host: str, user: str, excludes: list[str] | None = None) -> None:
    """Stream a local directory to a remote directory via tar over asyncssh."""

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if excludes:
            tar_path = Path(tarinfo.name)
            for excl in excludes:
                if fnmatch.fnmatch(tar_path.name, excl) or fnmatch.fnmatch(tarinfo.name, excl):
                    return None
        return tarinfo

    buf = io.BytesIO()
    with tarfile.open(mode="w:gz", fileobj=buf) as tar:
        tar.add(str(src_dir), arcname=".", filter=_filter)
    tar_bytes = buf.getvalue()

    async def _async_tar() -> None:
        async with asyncssh.connect(host, username=user, known_hosts=None) as conn:
            await conn.run(f"mkdir -p '{remote_dest}'", check=True)
            proc = await conn.create_process(f"tar -xzf - -C '{remote_dest}'")
            proc.stdin.write(tar_bytes)
            await proc.stdin.drain()
            proc.stdin.write_eof()
            await proc.wait()
            if proc.exit_status != 0:
                raise RuntimeError(f"tar extract on remote failed with code {proc.exit_status}")

    asyncio.run(_async_tar())


def resolve_vm_ip(preferred: str) -> str:
    """Resolve active VM IP: preferred if reachable, else auto-detect from virbr0."""
    if _is_ssh_reachable(preferred):
        return preferred

    arp_path = Path("/proc/net/arp")
    if arp_path.exists():
        try:
            for line in arp_path.read_text(encoding="utf-8").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 6 and parts[5] == "virbr0" and parts[2] != "0x0":
                    candidate_ip = parts[0]
                    if _is_ssh_reachable(candidate_ip):
                        sys.stdout.write(f"[*] Auto-detected active VM IP on virbr0: {candidate_ip}\n")
                        sys.stdout.flush()
                        return candidate_ip
        except OSError as exc:
            sys.stderr.write(f"[*] ARP table probe failed ({exc}), falling back to {preferred}\n")
    return preferred


def push_file(local_file: Path, remote_dest: str, host: str, user: str, mode: str | None = None) -> None:
    """Copy a single file to remote destination and optionally set permissions via asyncssh SFTP."""
    sys.stdout.write(f"[*] Pushing: {local_file} -> {remote_dest}\n")
    sys.stdout.flush()

    async def _async_push() -> None:
        async with (
            asyncssh.connect(host, username=user, known_hosts=None) as conn,
            conn.start_sftp_client() as sftp,
        ):
            parent_dir = str(Path(remote_dest).parent)
            await sftp.makedirs(parent_dir, exist_ok=True)
            await sftp.put(str(local_file), remote_dest)
            if mode:
                await sftp.chmod(remote_dest, int(mode, 8))

    asyncio.run(_async_push())


@app.command()
def sync(
    host: Annotated[str, typer.Option("--host", "-H", help="Target OpenWrt IP or hostname")] = "192.168.122.200",
    user: Annotated[str, typer.Option("--user", "-u", help="SSH user")] = "root",
    restart: Annotated[bool, typer.Option("--restart", "-r", help="Restart mcubridge service after sync")] = True,
) -> None:
    """Synchronize all canonical McuBridge code into the running VM."""
    target_host = resolve_vm_ip(host)
    print("========================================================")
    print(" McuBridge VM Code Synchronizer")
    print(f" Target: {user}@{target_host}")
    print("========================================================")

    # 0. Ensure protocol definitions, defaults and schemas are generated
    print("\n[0/7] Ensuring generated protocol definitions and schemas are up-to-date...")
    from tools.protocol.generate import main as generate_protocol

    generate_protocol(spec_file=REPO_ROOT / "tools" / "protocol" / "mcubridge.proto")

    # 1. Sync mcubridge python package
    pkg_src = REPO_ROOT / "mcubridge" / "mcubridge"
    pkg_dst = "/usr/lib/python3.13/site-packages/mcubridge"
    print(f"\n[1/7] Syncing Python package: {pkg_src} -> {pkg_dst}")
    tar_push(
        src_dir=pkg_src,
        remote_dest=pkg_dst,
        host=target_host,
        user=user,
        excludes=["__pycache__", "*.pyc", "*.pyo"],
    )

    # 2. Sync init script
    init_src = REPO_ROOT / "mcubridge" / "mcubridge.init"
    print(f"\n[2/7] Syncing init script: {init_src} -> /etc/init.d/mcubridge")
    push_file(init_src, "/etc/init.d/mcubridge", host=target_host, user=user, mode="0755")

    # 3. Sync gateway binary
    gateway_src = REPO_ROOT / "mcubridge-gateway" / "gateway.py"
    print(f"\n[3/7] Syncing Gateway: {gateway_src} -> /usr/bin/mcubridge-gateway")
    push_file(gateway_src, "/usr/bin/mcubridge-gateway", host=target_host, user=user, mode="0755")

    # 4. Sync helper scripts and default environment
    scripts_dir = REPO_ROOT / "mcubridge" / "scripts"
    print(f"\n[4/7] Syncing scripts from {scripts_dir}...")
    push_file(
        scripts_dir / "mcubridge_file_push.py", "/usr/bin/mcubridge-file-push", host=target_host, user=user, mode="0755"
    )
    push_file(
        scripts_dir / "mcubridge_rotate_credentials.py",
        "/usr/bin/mcubridge-rotate-credentials",
        host=target_host,
        user=user,
        mode="0755",
    )
    push_file(scripts_dir / "pin_rest_cgi.py", "/usr/bin/pin-rest-cgi", host=target_host, user=user, mode="0755")
    push_file(scripts_dir / "pin_rest_cgi.sh", "/www/cgi-bin/mcubridge-pin", host=target_host, user=user, mode="0755")
    push_file(scripts_dir / "defaults.sh", "/usr/share/mcubridge/defaults.sh", host=target_host, user=user, mode="0644")

    # 5. Sync LuCI files
    luci_dir = REPO_ROOT / "luci-app-mcubridge"
    print(f"\n[5/7] Syncing LuCI application files from {luci_dir}...")
    luci_root = luci_dir / "root"
    if luci_root.exists():
        tar_push(src_dir=luci_root, remote_dest="/", host=target_host, user=user)

    luci_htdocs = luci_dir / "htdocs"
    if luci_htdocs.exists():
        tar_push(src_dir=luci_htdocs, remote_dest="/www", host=target_host, user=user)

    # 6. Sync client test examples
    examples_dir = REPO_ROOT / "mcubridge-client-examples"
    examples_dst = "/tmp/mcubridge-client-examples"
    print(f"\n[6/7] Syncing client examples: {examples_dir} -> {examples_dst}")
    tar_push(src_dir=examples_dir, remote_dest=examples_dst, host=target_host, user=user)

    # 7. Sync pure Python runtime dependencies (statemachine, anyio, sniffio, idna)
    print("\n[7/7] Syncing pure Python runtime dependencies...")
    import importlib.util

    for mod_name in ["statemachine", "anyio", "sniffio", "idna"]:
        try:
            spec = importlib.util.find_spec(mod_name)
            if spec and spec.origin:
                origin_path = Path(spec.origin)
                src_path = origin_path.parent if origin_path.name == "__init__.py" else origin_path
                dst_path = f"/usr/lib/python3.13/site-packages/{src_path.name}"
                print(f"[*] Syncing {mod_name} -> {dst_path}")
                if src_path.is_dir():
                    tar_push(
                        src_dir=src_path,
                        remote_dest=dst_path,
                        host=target_host,
                        user=user,
                        excludes=["__pycache__", "*.pyc", "*.pyo"],
                    )
                else:
                    push_file(src_path, dst_path, host=target_host, user=user)
        except (ImportError, OSError, RuntimeError) as exc:
            print(f"[!] Warning syncing dependency {mod_name}: {exc}")

    # Clean old bytecode and LuCI caches on remote
    print("\n[*] Cleaning stale remote .pyc bytecode and LuCI caches...")
    run_remote_ssh(
        (
            "find /usr/lib/python3.13/site-packages -name '*.pyc' -delete 2>/dev/null || true; "
            "rm -rf /tmp/luci-* 2>/dev/null || true"
        ),
        host=target_host,
        user=user,
        check=True,
    )

    # Ensure UCI configuration matches hardware setup
    print("\n[*] Verifying UCI configuration on remote VM...")
    run_remote_ssh(
        (
            "uci -q set mcubridge.general.serial_port='/dev/ttyS1' && "
            "uci -q set mcubridge.general.serial_shared_secret="
            "'8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe' && "
            "uci -q set mcubridge.general.cloud_enabled='0' && "
            "uci -q set mcubridge.general.debug='1' && "
            "uci -q commit mcubridge"
        ),
        host=target_host,
        user=user,
        check=True,
    )

    if restart:
        print("\n[*] Restarting mcubridge and web services...")
        run_remote_ssh(
            (
                "killall -9 python3 2>/dev/null || true; "
                "/etc/init.d/mcubridge restart; "
                "/etc/init.d/rpcd restart; "
                "/etc/init.d/uhttpd restart"
            ),
            host=target_host,
            user=user,
            check=True,
        )

    print("\n✅ Synchronization complete.")


if __name__ == "__main__":
    app()
