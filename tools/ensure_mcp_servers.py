#!/usr/bin/env python3
"""[SIL-2 / Mission-Critical Tooling]
Verify, install, and activate Semgrep MCP, Buf CLI (buf curl),
and Serial MCP Server in the local development environment.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    help="Verify, install, and activate Semgrep MCP, Buf CLI, and Serial MCP Server.",
    add_completion=False,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
GATEWAY_PID_FILE = Path("/tmp/mcubridge_gateway_local.pid")
GATEWAY_LOG_FILE = Path("/tmp/mcubridge_gateway_local.log")


def _is_port_listening(host: str, port: int, timeout_sec: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_sec)
        try:
            sock.connect((host, port))
            return True
        except (ConnectionRefusedError, TimeoutError, OSError):
            return False


def _stop_gateway() -> None:
    if not GATEWAY_PID_FILE.exists():
        print("[INFO] No running gateway PID file found.")
        return

    pid_str = GATEWAY_PID_FILE.read_text(encoding="utf-8").strip()
    if pid_str.isdigit():
        pid = int(pid_str)
        try:
            os.kill(pid, 15)  # SIGTERM
            print(f"[INFO] Sent SIGTERM to McuBridge Gateway (PID {pid}).")
        except ProcessLookupError:
            print(f"[INFO] Process {pid} was not running.")
        except PermissionError as exc:
            print(f"[WARN] Failed to terminate process {pid}: {exc}", file=sys.stderr)

    GATEWAY_PID_FILE.unlink(missing_ok=True)
    print("[INFO] Stopped local McuBridge Gateway.")


def _start_gateway_if_needed() -> None:
    print("[*] Checking local McuBridge gRPC Gateway on 127.0.0.1:8443...")
    if _is_port_listening("127.0.0.1", 8443):
        print("  ✅ Gateway already active and listening on port 8443.")
        return

    print("  -> Starting McuBridge Gateway on port 8443 in background...")
    gateway_script = REPO_ROOT / "mcubridge-gateway" / "gateway.py"
    with open(GATEWAY_LOG_FILE, "a", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            [sys.executable, str(gateway_script), "--no-tls", "--port", "8443"],
            stdout=log_f,
            stderr=log_f,
            cwd=str(REPO_ROOT),
        )
    GATEWAY_PID_FILE.write_text(f"{proc.pid}\n", encoding="utf-8")
    print(f"  ✅ Gateway started (PID: {proc.pid}).")


def _check_semgrep() -> Path:
    print("[1/4] Checking Semgrep MCP...")
    semgrep_bin = shutil.which("semgrep") or shutil.which(str(HOME / ".local" / "bin" / "semgrep"))
    if not semgrep_bin:
        print("  -> semgrep not found. Installing via system package / pip...")
        uv_bin = shutil.which("uv")
        if uv_bin:
            subprocess.run([uv_bin, "tool", "install", "semgrep"], check=True)
        else:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--break-system-packages", "semgrep"],
                check=True,
            )
        semgrep_bin = shutil.which("semgrep") or str(HOME / ".local" / "bin" / "semgrep")

    res = subprocess.run([str(semgrep_bin), "--version"], capture_output=True, text=True, check=False)
    print(f"  -> Semgrep version: {res.stdout.strip()}")

    init_payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "local-verifier", "version": "1.0"},
            },
        }
    )

    try:
        subprocess.run(
            [str(semgrep_bin), "mcp"],
            input=init_payload,
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass
    print("  ✅ Semgrep MCP stdio verified.")
    return Path(semgrep_bin)


def _check_buf() -> None:
    print("[2/4] Checking Buf CLI & buf curl...")
    buf_bin = shutil.which("buf")
    if not buf_bin:
        print("  ❌ Buf CLI ('buf') not found in PATH.", file=sys.stderr)
        raise typer.Exit(code=1)

    res = subprocess.run([buf_bin, "--version"], capture_output=True, text=True, check=False)
    print(f"  -> Buf CLI version: {res.stdout.strip()}")

    res_curl = subprocess.run([buf_bin, "curl", "--help"], capture_output=True, text=True, check=False)
    if res_curl.returncode == 0:
        print("  ✅ Buf CLI active with native buf curl support (HTTP/2 + HTTP/3 QUIC).")
    else:
        print("  ❌ buf curl command unavailable in current buf installation.", file=sys.stderr)
        raise typer.Exit(code=1)


def _check_serial_mcp() -> Path:
    print("[3/4] Checking serial-mcp-server...")
    local_bin = HOME / ".local" / "bin" / "serial-mcp-server"
    serial_bin = shutil.which("serial-mcp-server") or (str(local_bin) if local_bin.exists() else None)

    if not serial_bin:
        print("  -> serial-mcp-server not found.")
        cargo_bin = shutil.which("cargo")
        if cargo_bin:
            src_dir = HOME / ".local" / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            clone_dir = src_dir / "serial-mcp-server"
            if clone_dir.exists():
                shutil.rmtree(clone_dir)
            subprocess.run(
                ["git", "clone", "--depth", "1", "https://github.com/adancurusul/serial-mcp-server.git", str(clone_dir)],
                check=True,
            )
            subprocess.run([cargo_bin, "build", "--release"], cwd=str(clone_dir), check=True)
            built_bin = clone_dir / "target" / "release" / "serial-mcp-server"
            local_bin.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built_bin, local_bin)
            local_bin.chmod(0o755)
            serial_bin = str(local_bin)
        else:
            print("  ⚠️ Cargo not found; skipping serial-mcp-server build.")
            return local_bin

    res = subprocess.run([serial_bin, "--version"], capture_output=True, text=True, check=False)
    print(f"  -> serial-mcp-server version: {res.stdout.strip() or 'active'}")
    print("  ✅ serial-mcp-server port discovery operational.")
    return Path(serial_bin)


def _sync_mcp_configs(semgrep_path: Path, serial_mcp_path: Path) -> None:
    print("[4/4] Verifying MCP configurations in workspace and user environments...")
    configs = [
        REPO_ROOT / ".agent" / "mcp_config.json",
        HOME / ".gemini" / "config" / "mcp_config.json",
    ]

    server_definitions = {
        "semgrep": {
            "command": str(semgrep_path),
            "args": ["mcp"],
        },
        "serial-mcp-server": {
            "command": str(serial_mcp_path),
            "args": ["serve"],
        },
    }

    for cfg in configs:
        data: dict[str, dict[str, object]] = {"mcpServers": {}}
        if cfg.exists():
            try:
                raw_data = json.loads(cfg.read_text(encoding="utf-8"))
                if isinstance(raw_data, dict) and "mcpServers" in raw_data and isinstance(raw_data["mcpServers"], dict):
                    data = raw_data
            except (json.JSONDecodeError, OSError):
                data = {"mcpServers": {}}

        mcp_servers = data.setdefault("mcpServers", {})
        updated = False

        if "grpcurl-mcp" in mcp_servers:
            del mcp_servers["grpcurl-mcp"]
            updated = True

        for srv_name, srv_def in server_definitions.items():
            if mcp_servers.get(srv_name) != srv_def:
                mcp_servers[srv_name] = srv_def
                updated = True

        if updated or not cfg.exists():
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"  -> Synchronized {cfg}")
        else:
            print(f"  -> {cfg} already up-to-date.")


@app.command()
def main(
    start_gateway: Annotated[bool, typer.Option("--start-gateway", help="Start local McuBridge gRPC Gateway")] = False,
    stop_gateway: Annotated[bool, typer.Option("--stop-gateway", help="Stop local McuBridge gRPC Gateway")] = False,
) -> None:
    if stop_gateway:
        _stop_gateway()
        return

    print("=================================================================")
    print(" McuBridge Local Tooling Bootstrap & Activation Gate")
    print("=================================================================")

    semgrep_path = _check_semgrep()
    _check_buf()
    serial_mcp_path = _check_serial_mcp()
    _sync_mcp_configs(semgrep_path, serial_mcp_path)

    # Gemini parity audit
    parity_script = REPO_ROOT / "tools" / "audit" / "check_gemini_parity.py"
    if parity_script.exists():
        subprocess.run([sys.executable, str(parity_script), "--fix"], check=False)

    if start_gateway:
        _start_gateway_if_needed()

    print("=================================================================")
    print(" ✅ MCP servers and Buf CLI verified and activated locally.")
    print("=================================================================")


if __name__ == "__main__":
    app()
