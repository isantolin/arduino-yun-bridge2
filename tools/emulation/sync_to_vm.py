#!/usr/bin/env python3
"""Synchronize repository files into the OpenWrt QEMU VM.

Copies the active source code, scripts, configuration, LuCI assets, and
client examples from the host repository directly into the running OpenWrt VM.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

REPO_ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(help="Synchronize local McuBridge source files into OpenWrt VM.", add_completion=False)


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[bytes]:
    sys.stdout.write(f"[*] Executing: {' '.join(cmd)}\n")
    sys.stdout.flush()
    return subprocess.run(cmd, check=check)


def tar_push(src_dir: Path, remote_dest: str, host: str, user: str, excludes: list[str] | None = None) -> None:
    """Stream a local directory to a remote directory via tar over SSH."""
    exclude_args: list[str] = []
    if excludes:
        for excl in excludes:
            exclude_args.extend(["--exclude", excl])

    ssh_mkdir = ["ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{host}", f"mkdir -p '{remote_dest}'"]
    run_cmd(ssh_mkdir, check=True)

    tar_create = subprocess.Popen(
        ["tar", "-C", str(src_dir), "-czf", "-"] + exclude_args + ["."],
        stdout=subprocess.PIPE,
    )
    ssh_extract = subprocess.Popen(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{host}", f"tar -xzf - -C '{remote_dest}'"],
        stdin=tar_create.stdout,
    )
    if tar_create.stdout:
        tar_create.stdout.close()
    ssh_extract.wait()
    tar_create.wait()

    if tar_create.returncode != 0:
        raise RuntimeError(f"tar create failed with code {tar_create.returncode}")
    if ssh_extract.returncode != 0:
        raise RuntimeError(f"tar extract on remote failed with code {ssh_extract.returncode}")


def push_file(local_file: Path, remote_dest: str, host: str, user: str, mode: str | None = None) -> None:
    """Copy a single file to remote destination and optionally set permissions."""
    ssh_cmd = f"cat > '{remote_dest}'"
    if mode:
        ssh_cmd += f" && chmod {mode} '{remote_dest}'"
    sys.stdout.write(f"[*] Pushing: {local_file} -> {remote_dest}\n")
    sys.stdout.flush()
    proc = subprocess.Popen(
        ["ssh", "-o", "StrictHostKeyChecking=no", f"{user}@{host}", ssh_cmd],
        stdin=subprocess.PIPE,
    )
    proc.communicate(input=local_file.read_bytes())
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to push {local_file} to {remote_dest} (code {proc.returncode})")


@app.command()
def sync(
    host: Annotated[str, typer.Option("--host", "-H", help="Target OpenWrt IP or hostname")] = "192.168.122.200",
    user: Annotated[str, typer.Option("--user", "-u", help="SSH user")] = "root",
    restart: Annotated[bool, typer.Option("--restart", "-r", help="Restart mcubridge service after sync")] = True,
) -> None:
    """Synchronize all canonical McuBridge code into the running VM."""
    print("========================================================")
    print(" McuBridge VM Code Synchronizer")
    print(f" Target: {user}@{host}")
    print("========================================================")

    # 1. Sync mcubridge python package
    pkg_src = REPO_ROOT / "mcubridge" / "mcubridge"
    pkg_dst = "/usr/lib/python3.13/site-packages/mcubridge"
    print(f"\n[1/6] Syncing Python package: {pkg_src} -> {pkg_dst}")
    tar_push(
        src_dir=pkg_src,
        remote_dest=pkg_dst,
        host=host,
        user=user,
        excludes=["__pycache__", "*.pyc", "*.pyo"],
    )

    # 2. Sync init script
    init_src = REPO_ROOT / "mcubridge" / "mcubridge.init"
    print(f"\n[2/6] Syncing init script: {init_src} -> /etc/init.d/mcubridge")
    push_file(init_src, "/etc/init.d/mcubridge", host=host, user=user, mode="0755")

    # 3. Sync gateway binary
    gateway_src = REPO_ROOT / "mcubridge-gateway" / "gateway.py"
    print(f"\n[3/6] Syncing Gateway: {gateway_src} -> /usr/bin/mcubridge-gateway")
    push_file(gateway_src, "/usr/bin/mcubridge-gateway", host=host, user=user, mode="0755")

    # 4. Sync helper scripts
    scripts_dir = REPO_ROOT / "mcubridge" / "scripts"
    print(f"\n[4/6] Syncing scripts from {scripts_dir}...")
    push_file(scripts_dir / "mcubridge_file_push.py", "/usr/bin/mcubridge-file-push", host=host, user=user, mode="0755")
    push_file(
        scripts_dir / "mcubridge_rotate_credentials.py",
        "/usr/bin/mcubridge-rotate-credentials",
        host=host,
        user=user,
        mode="0755",
    )
    push_file(scripts_dir / "pin_rest_cgi.py", "/usr/bin/pin-rest-cgi", host=host, user=user, mode="0755")
    push_file(scripts_dir / "pin_rest_cgi.py", "/www/cgi-bin/mcubridge-pin", host=host, user=user, mode="0755")

    # 5. Sync LuCI files
    luci_dir = REPO_ROOT / "luci-app-mcubridge"
    print(f"\n[5/6] Syncing LuCI application files from {luci_dir}...")
    luci_root = luci_dir / "root"
    if luci_root.exists():
        tar_push(src_dir=luci_root, remote_dest="/", host=host, user=user)

    luci_htdocs = luci_dir / "htdocs"
    if luci_htdocs.exists():
        tar_push(src_dir=luci_htdocs, remote_dest="/www", host=host, user=user)

    # 6. Sync client test examples
    examples_dir = REPO_ROOT / "mcubridge-client-examples"
    examples_dst = "/tmp/mcubridge-client-examples"
    print(f"\n[6/7] Syncing client examples: {examples_dir} -> {examples_dst}")
    tar_push(src_dir=examples_dir, remote_dest=examples_dst, host=host, user=user)

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
                        host=host,
                        user=user,
                        excludes=["__pycache__", "*.pyc", "*.pyo"],
                    )
                else:
                    push_file(src_path, dst_path, host=host, user=user)
        except (ImportError, OSError, RuntimeError) as exc:
            print(f"[!] Warning syncing dependency {mod_name}: {exc}")

    # Clean old bytecode on remote
    print("\n[*] Cleaning stale remote .pyc bytecode cache...")
    run_cmd(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            f"{user}@{host}",
            "find /usr/lib/python3.13/site-packages -name '*.pyc' -delete 2>/dev/null || true",
        ],
        check=True,
    )

    # Ensure UCI configuration matches hardware setup
    print("\n[*] Verifying UCI configuration on remote VM...")
    run_cmd(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            f"{user}@{host}",
            "uci -q set mcubridge.general.serial_port='/dev/ttyS1' && "
            "uci -q set mcubridge.general.serial_shared_secret="
            "'8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe' && "
            "uci -q set mcubridge.general.cloud_enabled='0' && "
            "uci -q set mcubridge.general.debug='1' && "
            "uci -q commit mcubridge",
        ],
        check=True,
    )

    if restart:
        print("\n[*] Restarting mcubridge service...")
        run_cmd(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                f"{user}@{host}",
                "killall -9 python3 2>/dev/null || true; /etc/init.d/mcubridge restart",
            ],
            check=True,
        )

    print("\n✅ Synchronization complete.")


if __name__ == "__main__":
    app()
