#!/usr/bin/env python3
"""
Hardware Emulation Runner.
Direct PTY-PTY link via socat, with MCU opening its PTY directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import structlog
import tenacity
import typer
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import protocol

from tools.emulation.process_utils import (
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
    wait_for_path_ready,
    wait_for_tcp_ready,
    write_fake_uci_module,
)

repo_root = Path(__file__).resolve().parents[2]

# --- Constants ---
SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = protocol.DEFAULT_CLOUD_PORT

configure_logging(console=True)
logger = structlog.get_logger("emulation-runner")


def _default_output_lines() -> list[tuple[str, str]]:
    return []


@dataclass
class EmulationState:
    output_lines: list[tuple[str, str]] = field(default_factory=_default_output_lines)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def on_line(self, line: str, source: str) -> None:
        clean_line = line.strip()
        if not clean_line:
            return
        with self.lock:
            self.output_lines.append((source, clean_line))
            logger.info("Process output", source=source, line=clean_line)


class CloudVerifier:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def wait_for_ready(self, timeout: float = 30.0) -> bool:
        return wait_for_tcp_ready(self.host, self.port, timeout=timeout)


def _start_cloud_gateway(cloud_verify: CloudVerifier, state: EmulationState) -> subprocess.Popen[str] | None:
    if cloud_verify.wait_for_ready(timeout=1.0):
        return None

    logger.info("Starting Managed Cloud Gateway...")
    gateway_env = dict(os.environ)
    gateway_env["PYTHONUNBUFFERED"] = "1"
    gateway_cmd = [
        sys.executable,
        "-u",
        str(repo_root / "mcubridge-gateway" / "gateway.py"),
        "--no-tls",
        "--port",
        str(CLOUD_PORT),
    ]
    gateway_proc = subprocess.Popen(
        gateway_cmd,
        env=gateway_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    start_daemon_thread(stream_pump, "gateway", gateway_proc.stdout, state.on_line, "gateway")

    if not cloud_verify.wait_for_ready():
        logger.error("Cloud Gateway not available")
        terminate_process_tree([gateway_proc], timeout=1.0)
        sys.exit(1)

    return gateway_proc


def _prepare_daemon_environment(p_root: Path, emulator_fs_root: Path) -> tuple[Path, dict[str, str], list[str]]:
    fake_uci_dir = Path(tempfile.mkdtemp(prefix="mcubridge_fake_uci_"))

    daemon_env = dict(os.environ)
    extra_paths = [
        str(fake_uci_dir),
        str(p_root / "mcubridge"),
        str(p_root / "mcubridge-client-examples"),
        str(p_root),
    ]
    curr_pp = daemon_env.get("PYTHONPATH", "")
    daemon_env["PYTHONPATH"] = ":".join(extra_paths + ([curr_pp] if curr_pp else []))
    daemon_env["PYTHONUNBUFFERED"] = "1"
    daemon_env["MCUBRIDGE_FORCE_UCI"] = "1"
    daemon_env["MCUBRIDGE_NON_INTERACTIVE"] = "1"
    daemon_env["MCUBRIDGE_LOG_STREAM"] = "1"
    daemon_env["MCUBRIDGE_SERIAL_PORT"] = SOCAT_PORT0
    daemon_env["MCUBRIDGE_SERIAL_SAFE_BAUD"] = str(protocol.DEFAULT_SAFE_BAUDRATE)
    daemon_env["MCUBRIDGE_SERIAL_BAUD"] = str(protocol.DEFAULT_BAUDRATE)
    daemon_env["MCUBRIDGE_DISABLE_METRICS"] = "1"
    daemon_env["MCUBRIDGE_CLOUD_ENABLED"] = "1"
    daemon_env["MCUBRIDGE_CLOUD_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_CLOUD_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_GATEWAY_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_GATEWAY_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_DEVICE_ID"] = "yun-01"
    daemon_env["MCUBRIDGE_STORAGE_PATH"] = tempfile.mkdtemp(prefix="mcubridge_db_")

    uci_config = {
        "serial_port": SOCAT_PORT0,
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "cloud_enabled": "1",
        "cloud_host": CLOUD_HOST,
        "cloud_port": str(CLOUD_PORT),
        "cloud_tls": "0",
        "cloud_tls_insecure": "1",
        "serial_shared_secret": "DEBUG_INSECURE",
        "allowed_commands": "*",
        "debug": "1",
        "disable_metrics": "1",
        "file_system_root": str(emulator_fs_root),
        "storage_path": daemon_env["MCUBRIDGE_STORAGE_PATH"],
    }
    write_fake_uci_module(fake_uci_dir, uci_config, label="e2e runner")

    daemon_cmd = [sys.executable, "-u"]
    if os.environ.get("COVERAGE_FILE"):
        daemon_cmd.extend(["-m", "coverage", "run", "--append", "--rcfile", str(p_root / "pyproject.toml")])
    daemon_cmd.extend(["-m", "mcubridge.daemon"])

    return fake_uci_dir, daemon_env, daemon_cmd


def run_emulation(
    firmware_path: Path,
    package_root: Path = Path(),
    run_scripts: list[str] | None = None,
):
    state = EmulationState()
    cloud_verify = CloudVerifier(CLOUD_HOST, CLOUD_PORT)
    gateway_proc = _start_cloud_gateway(cloud_verify, state)

    # 1. Start Unified socat linking PTY to MCU EXEC
    Path(SOCAT_PORT0).unlink(missing_ok=True)

    # [FIX] Ensure emulator filesystem root exists and is clean
    emulator_fs_root = Path("/tmp/mcubridge-host-fs")
    if emulator_fs_root.exists():
        import shutil

        try:
            shutil.rmtree(emulator_fs_root)
        except OSError as exc:
            logger.error("Failed to clean emulator FS root", path=str(emulator_fs_root), error=str(exc))
    emulator_fs_root.mkdir(parents=True, exist_ok=True)

    logger.info("Starting Unified MCU Emulator via socat EXEC...")
    # Use EXEC with default pipes. PTY is only created for the Daemon side.
    # start_new_session isolates socat from terminal SIGHUP signals.
    mcu_proc = subprocess.Popen(
        [
            "socat",
            "-d",
            "-d",
            f"PTY,link={SOCAT_PORT0},raw,echo=0",
            f"EXEC:{firmware_path.absolute()},pty,raw,echo=0",
        ],
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    start_daemon_thread(stream_pump, "mcu-socat", mcu_proc.stderr, state.on_line, "mcu")

    # Wait for PTY
    if not wait_for_path_ready(SOCAT_PORT0, timeout=10.0, interval=0.1):
        logger.error("Timeout waiting for unified PTY", path=SOCAT_PORT0)
        terminate_process_tree([mcu_proc], timeout=1.0)
        sys.exit(1)

    # 3. Start Daemon
    p_root = package_root.absolute()
    fake_uci_dir, daemon_env, daemon_cmd = _prepare_daemon_environment(p_root, emulator_fs_root)
    daemon_proc = None
    all_success = True

    try:
        logger.info("Starting Daemon...")
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            env=daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "daemon", daemon_proc.stdout, state.on_line, "daemon")

        # Wait for Daemon/MCU sync using declarative tenacity retry
        logger.info("Waiting for Daemon/MCU synchronization...")
        status_file = Path("/tmp/mcubridge_status.json")

        def _is_synced() -> bool:
            if not status_file.exists():
                return False
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                return bool(data.get("bridge", {}).get("is_synchronized", False))
            except (json.JSONDecodeError, OSError):
                return False

        sync_retryer = tenacity.Retrying(
            stop=tenacity.stop_after_delay(15.0),
            wait=tenacity.wait_fixed(0.25),
            retry=tenacity.retry_if_result(lambda ok: not ok),
            reraise=False,
        )
        try:
            is_synced = sync_retryer(_is_synced)
        except tenacity.RetryError:
            is_synced = False

        if is_synced:
            logger.info("Daemon/MCU synchronization established successfully.")
        else:
            logger.warning("Synchronization check timed out after 15s; proceeding with test execution.")

        # 4. Run scripts
        if run_scripts:
            for script in run_scripts:
                if not Path(script).exists():
                    logger.error("Script not found", script=script)
                    all_success = False
                    break

                with state.lock:
                    lines_before = len(state.output_lines)

                try:
                    # Run with captured output but echoing to parent stdout/stderr
                    subprocess.run(
                        [sys.executable, script, "--device-id", "yun-01"], env=daemon_env, check=True, timeout=60
                    )
                    logger.info("Script execution passed", script=script)
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                ) as exc:
                    logger.error("Script execution failed", script=script, error=str(exc))
                    all_success = False
                    break

                # [SIL-2 Log Audit] Verify that the daemon or MCU emitted zero error logs during test execution
                with state.lock:
                    new_lines = list(state.output_lines[lines_before:])
                script_errors = [
                    f"[{src}] {line}"
                    for src, line in new_lines
                    if '"level": "error"' in line or '"level":"error"' in line or "MCU > ERROR:" in line
                ]
                if script_errors:
                    logger.error(
                        "Script produced unexpected error log events",
                        script=script,
                        error_count=len(script_errors),
                    )
                    for err in script_errors:
                        logger.error("Unexpected script error", detail=err)
                    all_success = False
                    break

                # Small cool-down between scripts to keep logs separated
                time.sleep(1)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Emulation error", error=str(exc))
        all_success = False
    finally:
        procs_to_terminate = [p for p in (daemon_proc, mcu_proc, gateway_proc) if p is not None]
        terminate_process_tree(procs_to_terminate, timeout=2.0)

    if not all_success:
        logger.error("Emulation FAILED.")
        sys.exit(1)
    else:
        logger.info("Emulation SUCCESS.")


cli = typer.Typer(help="Hardware Emulation Runner", add_completion=False)


@cli.command()
def main(
    firmware: Annotated[Path, typer.Option("--firmware", help="Path to MCU firmware binary")],
    package_root: Annotated[Path, typer.Option("--package-root", help="Root of mcubridge package")] = Path(),
    run_scripts: Annotated[list[str] | None, typer.Argument(help="Client scripts to run")] = None,
) -> None:
    run_emulation(
        firmware_path=firmware,
        package_root=package_root,
        run_scripts=run_scripts or [],
    )


if __name__ == "__main__":
    cli()
