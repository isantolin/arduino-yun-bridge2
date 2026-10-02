#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Hardware Emulation Runner.

Direct PTY-PTY link via socat, with MCU opening its PTY directly.
Direct in-process client test execution and structured status health audit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, cast

import structlog
import tenacity
import typer
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import protocol

from tools.audit.audit_bridge_status import audit_status_dict
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


def default_output_lines() -> list[tuple[str, str]]:
    return []


@dataclass
class EmulationState:
    output_lines: list[tuple[str, str]] = field(default_factory=default_output_lines)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def on_line(self, line: str, source: str) -> None:
        clean_line = line.strip()
        if not clean_line:
            return
        with self.lock:
            self.output_lines.append((source, clean_line))
            logger.info("Process output", source=source, line=clean_line)


def ensure_cloud_gateway(state: EmulationState) -> subprocess.Popen[str] | None:
    """Ensure Cloud Gateway is running without redundant wrapper shims (Rule 2.1)."""
    if wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=1.0):
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

    if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=15.0):
        logger.error("Cloud Gateway not available after spawn")
        terminate_process_tree([gateway_proc], timeout=1.0)
        sys.exit(1)

    return gateway_proc


def prepare_emulator_fs(fs_root: Path) -> None:
    """Ensure emulator filesystem root directory is clean and prepared."""
    if fs_root.exists():
        try:
            shutil.rmtree(fs_root)
        except OSError as exc:
            logger.error("Failed to clean emulator FS root", path=str(fs_root), error=str(exc))
    fs_root.mkdir(parents=True, exist_ok=True)


def _prepare_daemon_environment(p_root: Path, emulator_fs_root: Path) -> tuple[dict[str, str], list[str]]:
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

    return daemon_env, daemon_cmd


def wait_for_daemon_sync(status_file: Path, timeout: float = 15.0) -> bool:
    """Wait for Daemon/MCU synchronization using declarative tenacity retry."""

    def _is_synced() -> bool:
        if not status_file.exists():
            return False
        try:
            data = cast(dict[str, dict[str, object]], json.loads(status_file.read_text(encoding="utf-8")))
            bridge = data.get("bridge")
            return isinstance(bridge, dict) and bridge.get("is_synchronized") is True
        except (json.JSONDecodeError, OSError):
            return False

    sync_retryer = tenacity.Retrying(
        stop=tenacity.stop_after_delay(timeout),
        wait=tenacity.wait_fixed(0.25),
        retry=tenacity.retry_if_result(lambda ok: not ok),
        reraise=False,
    )
    try:
        return sync_retryer(_is_synced)
    except tenacity.RetryError:
        return False


def run_client_script(script_path: Path, device_id: str = "yun-01") -> bool:
    """Execute client test script directly in-process via module main or runpy (Rule 37)."""
    logger.info("Executing client test in-process", script=script_path.name)
    try:
        spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            main_fn = getattr(mod, "main", None)
            if callable(main_fn):
                main_fn(host=CLOUD_HOST, port=CLOUD_PORT, device_id=device_id)
                logger.info("Script execution passed", script=script_path.name)
                return True

        orig_argv = sys.argv[:]
        try:
            sys.argv = [str(script_path), "--device-id", device_id]
            runpy.run_path(str(script_path), run_name="__main__")
        finally:
            sys.argv = orig_argv

        logger.info("Script execution passed", script=script_path.name)
        return True
    except SystemExit as exc:
        if exc.code in (0, None):
            logger.info("Script execution passed", script=script_path.name)
            return True
        logger.error("Script exited with error", script=script_path.name, code=exc.code)
        return False
    except (OSError, RuntimeError, ValueError, TypeError, AssertionError, TimeoutError) as exc:
        logger.error("Script execution failed", script=script_path.name, error=str(exc))
        return False


def audit_post_execution_status(status_file: Path) -> bool:
    """Audit runtime status snapshot and active system health (SIL-2 / Rule 29)."""
    if not status_file.exists():
        logger.warning("Status file does not exist for post-execution audit", path=str(status_file))
        return True

    try:
        status_data = json.loads(status_file.read_text(encoding="utf-8"))
        if not isinstance(status_data, dict):
            logger.error("Status file root is not a dictionary")
            return False

        status_errors = audit_status_dict(cast(dict[str, Any], status_data))
        if status_errors:
            logger.error("Post-execution status health check failed", errors=status_errors)
            for err in status_errors:
                logger.error("Status audit anomaly", error=err)
            return False

        logger.info("Post-execution status health check passed (100% clean)")
        return True
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error("Failed auditing bridge status", error=str(exc))
        return False


def run_emulation(
    firmware_path: Path,
    package_root: Path = Path(),
    run_scripts: list[str] | None = None,
) -> None:
    state = EmulationState()
    gateway_proc = ensure_cloud_gateway(state)

    # 1. Start Unified socat linking PTY to MCU EXEC
    Path(SOCAT_PORT0).unlink(missing_ok=True)

    emulator_fs_root = Path("/tmp/mcubridge-host-fs")
    prepare_emulator_fs(emulator_fs_root)

    logger.info("Starting Unified MCU Emulator via socat EXEC...")
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
    daemon_env, daemon_cmd = _prepare_daemon_environment(p_root, emulator_fs_root)
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

        # Wait for Daemon/MCU sync
        logger.info("Waiting for Daemon/MCU synchronization...")
        status_file = Path("/tmp/mcubridge_status.json")
        is_synced = wait_for_daemon_sync(status_file, timeout=15.0)

        if is_synced:
            logger.info("Daemon/MCU synchronization established successfully.")
        else:
            logger.warning("Synchronization check timed out after 15s; proceeding with test execution.")

        # 4. Run scripts via in-process library execution
        if run_scripts:
            for script in run_scripts:
                s_path = Path(script)
                if not s_path.exists():
                    logger.error("Script not found", script=script)
                    all_success = False
                    break

                with state.lock:
                    lines_before = len(state.output_lines)

                passed = run_client_script(s_path, device_id="yun-01")
                if not passed:
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

                # Small cool-down between scripts
                time.sleep(1)

        # 5. [SIL-2 / Rule 29] Mandatory Post-Execution Status Audit
        if all_success:
            status_clean = audit_post_execution_status(status_file)
            if not status_clean:
                all_success = False
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Emulation error", error=str(exc))
        all_success = False
    finally:
        procs_to_terminate: list[subprocess.Popen[Any]] = [
            p for p in (daemon_proc, mcu_proc, gateway_proc) if p is not None
        ]
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
