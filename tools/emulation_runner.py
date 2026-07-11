#!/usr/bin/env python3
"""
Hardware Emulation Runner.
Direct PTY-PTY link via socat, with MCU opening its PTY directly.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from mcubridge.protocol import protocol
except ModuleNotFoundError as exc:
    # CI can invoke this script without tox's PYTHONPATH wiring.
    if exc.name not in {"mcubridge", "mcubridge.protocol"}:
        raise
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root / "mcubridge"))
    sys.path.insert(0, str(repo_root / "mcubridge-client-examples"))
    sys.path.insert(0, str(repo_root))
    from mcubridge.protocol import protocol

import argparse
import json
import tempfile

# --- Constants ---
SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = 8443

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("emulation-runner")


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
            logger.info("[%s] %s", source, clean_line)


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
            logger.warning("Could not unlink existing PTY %s: %s", SOCAT_PORT0, exc)

    # [FIX] Ensure emulator filesystem root exists and is clean
    emulator_fs_root = Path("/tmp/mcubridge-host-fs")
    if emulator_fs_root.exists():
        import shutil

        try:
            shutil.rmtree(emulator_fs_root)
        except OSError as exc:
            logger.error("Failed to clean emulator FS root %s: %s", emulator_fs_root, exc)
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
            f"EXEC:{firmware_path.absolute()}",
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
            logger.error("Timeout waiting for unified PTY %s", SOCAT_PORT0)
            mcu_proc.terminate()
            sys.exit(1)
        time.sleep(0.1)

    # 3. Start Daemon
    p_root = package_root.absolute()
    all_success = True
    daemon_proc: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="mcubridge-e2e-uci-", dir="/tmp") as uci_tmp:
            uci_dir = Path(uci_tmp)
            _write_fake_uci_module(
                uci_dir,
                {
                    "serial_port": SOCAT_PORT0,
                    "serial_baud": str(protocol.DEFAULT_BAUDRATE),
                    "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
                    "cloud_host": CLOUD_HOST,
                    "cloud_port": str(CLOUD_PORT),
                    "cloud_tls": "0",
                    "cloud_tls_insecure": "1",
                    "serial_shared_secret": "DEBUG_INSECURE",
                    "allowed_commands": "*",
                    "debug": "1",
                },
            )

            daemon_env = {
                **os.environ,
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": f"{uci_dir}:{p_root}:{p_root}/mcubridge:{p_root}/mcubridge-client-examples",
                "MCUBRIDGE_FORCE_UCI": "1",
                "MCUBRIDGE_NON_INTERACTIVE": "1",
                "MCUBRIDGE_LOG_STREAM": "1",
                "MCUBRIDGE_SOCKET_PATH": os.environ.get("MCUBRIDGE_SOCKET_PATH") or str(uci_dir / "mcubridge.sock"),
            }

            logger.info("Starting Daemon...")
            daemon_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "mcubridge.daemon",
                ],
                env=daemon_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            _start_worker_thread(_daemon_worker, "daemon-worker", daemon_proc, state)

            # 4. Run Test
            logger.info("Waiting for stability (15s)...")
            time.sleep(15)
            if run_scripts:
                for script in run_scripts:
                    sys.stdout.write(f"\n{'=' * 80}\n")
                    sys.stdout.write(f"=== RUNNING E2E TEST: {script}\n")
                    sys.stdout.write(f"{'=' * 80}\n\n")
                    sys.stdout.flush()

                    try:
                        # Run with captured output but echoing to parent stdout/stderr
                        subprocess.run([sys.executable, script], env=daemon_env, check=True, timeout=60)
                        logger.info("Script %s PASSED.", script)
                    except (
                        subprocess.CalledProcessError,
                        subprocess.TimeoutExpired,
                    ) as exc:
                        logger.error("Script %s FAILED: %s", script, exc)
                        all_success = False
                        break

                    # Small cool-down between scripts to keep logs separated
                    time.sleep(1)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Emulation error: %s", exc)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hardware Emulation Runner")
    parser.add_argument("--firmware", type=Path, required=True, help="Path to MCU firmware binary")
    parser.add_argument(
        "--package-root",
        type=Path,
        default=Path("."),
        help="Root of mcubridge package",
    )
    parser.add_argument("run_scripts", nargs="*", help="Client scripts to run")
    args = parser.parse_args()

    run_emulation(
        firmware_path=args.firmware,
        package_root=args.package_root,
        run_scripts=args.run_scripts,
    )
