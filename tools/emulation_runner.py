#!/usr/bin/env python3
"""
Hardware Emulation Runner.
Direct PTY-PTY link via socat, with MCU opening its PTY directly.
"""

from __future__ import annotations
from mcubridge.config.logging import configure_logging
import typer
import structlog
from typing import Annotated, Any
from dataclasses import dataclass, field
import time
import threading
import tempfile
import subprocess
import signal
import os
import json
import contextlib

import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root / "mcubridge") not in sys.path:
    sys.path.insert(0, str(repo_root / "mcubridge"))
if str(repo_root / "mcubridge-client-examples") not in sys.path:
    sys.path.insert(0, str(repo_root / "mcubridge-client-examples"))
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


# --- Constants ---
SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = 8443

configure_logging(console=True)
logger = structlog.get_logger("emulation-runner")


@dataclass
class EmulationState:
    output_lines: list[tuple[str, str]] = field(default_factory=lambda: [])
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
        import socket

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                with socket.create_connection((self.host, self.port), timeout=1):
                    return True
            except (OSError, ConnectionRefusedError):
                time.sleep(0.5)
        return False


def _start_worker_thread(target: Any, name: str, *args: Any) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, args=args, daemon=True)
    thread.start()
    return thread


def _mcu_stderr_worker(mcu_proc: subprocess.Popen[bytes], state: EmulationState) -> None:
    if mcu_proc.stderr:
        for line in iter(mcu_proc.stderr.readline, b""):
            if not line:
                break
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError:
                decoded = f"<hex:{line.hex()}>"
            state.on_line(decoded, "mcu")


def _daemon_worker(daemon_proc: subprocess.Popen[str], state: EmulationState) -> None:
    if daemon_proc.stdout:
        for line in iter(daemon_proc.stdout.readline, ""):
            if not line:
                break
            state.on_line(line, "daemon")


def _write_fake_uci_module(base_dir: Path, config: dict[str, str]) -> Path:
    module_path = base_dir / "uci.py"
    module_source = (
        "from __future__ import annotations\n"
        "from typing import Any\n\n"
        f"_CONFIG = {json.dumps(config, sort_keys=True)!r}\n\n"
        "class Uci:\n"
        "    def __enter__(self) -> 'Uci':\n"
        "        return self\n\n"
        "    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:\n"
        "        return False\n\n"
        "    def get_all(self, package: str, section: str | None = None) -> dict[str, str]:\n"
        "        if package != 'mcubridge':\n"
        "            return {}\n"
        "        if section not in (None, 'general'):\n"
        "            return {}\n"
        "        return dict(__import__('json').loads(_CONFIG))\n\n"
        "    def get(self, package: str, section: str, option: str) -> str:\n"
        "        return self.get_all(package, section)[option]\n\n"
        "    def set(self, package: str, section: str, option: str, value: str) -> None:\n"
        "        raise RuntimeError('fake UCI is read-only in e2e runner')\n\n"
        "    def commit(self, package: str) -> None:\n"
        "        return None\n\n"
        "class UCI(Uci):\n"
        "    pass\n"
    )
    module_path.write_text(module_source, encoding="utf-8")
    return module_path


def run_emulation(
    firmware_path: Path,
    package_root: Path = Path("."),
    run_scripts: list[str] | None = None,
):
    state = EmulationState()
    cloud_verify = CloudVerifier(CLOUD_HOST, CLOUD_PORT)

    if not cloud_verify.wait_for_ready():
        logger.error("Cloud Gateway not available")
        sys.exit(1)

    # 1. Start Unified socat linking PTY to MCU EXEC
    if os.path.exists(SOCAT_PORT0):
        try:
            os.unlink(SOCAT_PORT0)
        except OSError as exc:
            logger.warning("Could not unlink existing PTY", path=SOCAT_PORT0, error=str(exc))

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
    _start_worker_thread(_mcu_stderr_worker, "mcu-socat", mcu_proc, state)

    # Wait for PTY
    start = time.monotonic()
    while not os.path.exists(SOCAT_PORT0):
        if time.monotonic() - start > 10.0:
            logger.error("Timeout waiting for unified PTY", path=SOCAT_PORT0)
            mcu_proc.terminate()
            sys.exit(1)
        time.sleep(0.1)

    # 3. Start Daemon
    p_root = package_root.absolute()
    fake_uci_dir = Path(tempfile.mkdtemp(prefix="mcubridge_fake_uci_"))
    socket_path = str(fake_uci_dir / "mcubridge.sock")

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
    daemon_env["MCUBRIDGE_SOCKET_PATH"] = socket_path
    daemon_env["MCUBRIDGE_SERIAL_PORT"] = SOCAT_PORT0
    daemon_env["MCUBRIDGE_SERIAL_SAFE_BAUD"] = "115200"
    daemon_env["MCUBRIDGE_SERIAL_BAUD"] = "115200"
    daemon_env["MCUBRIDGE_DISABLE_METRICS"] = "1"
    daemon_env["MCUBRIDGE_CLOUD_ENABLED"] = "1"
    daemon_env["MCUBRIDGE_CLOUD_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_CLOUD_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_STORAGE_PATH"] = tempfile.mkdtemp(prefix="mcubridge_db_")

    uci_config = {
        "serial_port": SOCAT_PORT0,
        "serial_baud": "115200",
        "serial_safe_baud": "115200",
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
        "socket_path": socket_path,
    }
    _write_fake_uci_module(fake_uci_dir, uci_config)

    daemon_cmd = [sys.executable, "-u"]
    if os.environ.get("COVERAGE_FILE"):
        daemon_cmd.extend(["-m", "coverage", "run", "--append", "--rcfile", str(p_root / "pyproject.toml")])
    daemon_cmd.extend(["-m", "mcubridge.daemon"])
    daemon_proc = None
    all_success = True

    try:
        with contextlib.ExitStack():
            logger.info("Starting Daemon...")
            daemon_proc = subprocess.Popen(
                daemon_cmd,
                env=daemon_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            _start_worker_thread(_daemon_worker, "daemon", daemon_proc, state)

            # Wait for Daemon/MCU sync
            logger.info("Waiting for stability (15s)...")
            time.sleep(15)

            # 4. Run scripts
            if run_scripts:
                for script in run_scripts:
                    if not os.path.exists(script):
                        logger.error("Script not found", script=script)
                        all_success = False
                        break

                    with state.lock:
                        lines_before = len(state.output_lines)

                    try:
                        # Run with captured output but echoing to parent stdout/stderr
                        subprocess.run([sys.executable, script], env=daemon_env, check=True, timeout=60)
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
        # Terminate daemon (same process group — plain kill only)
        for p in [daemon_proc]:
            if p is None:
                continue
            with contextlib.suppress(Exception):
                os.kill(p.pid, signal.SIGTERM)
                p.wait(timeout=2)
            with contextlib.suppress(Exception):
                os.kill(p.pid, signal.SIGKILL)
        # Terminate socat+MCU (separate session — use process group)
        for p in [mcu_proc]:
            with contextlib.suppress(Exception):
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    os.kill(p.pid, signal.SIGTERM)
                p.wait(timeout=2)
            with contextlib.suppress(Exception):
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    os.kill(p.pid, signal.SIGKILL)

    if not all_success:
        logger.error("Emulation FAILED.")
        sys.exit(1)
    else:
        logger.info("Emulation SUCCESS.")


cli = typer.Typer(help="Hardware Emulation Runner", add_completion=False)


@cli.command()
def main(
    firmware: Annotated[Path, typer.Option("--firmware", help="Path to MCU firmware binary")],
    package_root: Annotated[Path, typer.Option("--package-root", help="Root of mcubridge package")] = Path("."),
    run_scripts: Annotated[list[str] | None, typer.Argument(help="Client scripts to run")] = None,
) -> None:
    run_emulation(
        firmware_path=firmware,
        package_root=package_root,
        run_scripts=run_scripts or [],
    )


if __name__ == "__main__":
    cli()
