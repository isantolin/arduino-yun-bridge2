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
from typing import Annotated, Any

import typer

# --- Constants ---
SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883

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


class MqttVerifier:
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


def _mcu_stderr_worker(
    mcu_proc: subprocess.Popen[bytes], state: EmulationState
) -> None:
    if mcu_proc.stderr:
        for line in iter(mcu_proc.stderr.readline, b""):
            if not line:
                break
            decoded = line.decode("utf-8", errors="ignore")
            state.on_line(decoded, "mcu")


def _daemon_worker(daemon_proc: subprocess.Popen[str], state: EmulationState) -> None:
    if daemon_proc.stdout:
        for line in iter(daemon_proc.stdout.readline, ""):
            if not line:
                break
            state.on_line(line, "daemon")


def main(
    firmware_path: Annotated[
        Path, typer.Option("--firmware", help="Path to MCU firmware binary")
    ],
    package_root: Annotated[
        Path, typer.Option("--package-root", help="Root of mcubridge package")
    ] = Path("."),
    run_scripts: Annotated[
        list[str] | None, typer.Argument(help="Client scripts to run")
    ] = None,
):
    state = EmulationState()
    mqtt = MqttVerifier(MQTT_HOST, MQTT_PORT)

    if not mqtt.wait_for_ready():
        logger.error("MQTT broker not available")
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
            logger.error(
                "Failed to clean emulator FS root %s: %s", emulator_fs_root, exc
            )
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
    daemon_env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": f"{p_root}:{p_root}/mcubridge:{p_root}/mcubridge-client-examples",
        "MCUBRIDGE_SERIAL_PORT": SOCAT_PORT0,
        "MCUBRIDGE_SERIAL_BAUD": "115200",
        "MCUBRIDGE_MQTT_HOST": MQTT_HOST,
        "MCUBRIDGE_MQTT_PORT": str(MQTT_PORT),
        "MCUBRIDGE_MQTT_TLS": "0",
        "MCUBRIDGE_SERIAL_SHARED_SECRET": "DEBUG_INSECURE",
        "MCUBRIDGE_LOG_LEVEL": "DEBUG",
        "MCUBRIDGE_LOG_STREAM": "1",
        "MCUBRIDGE_ALLOWED_COMMANDS": "*",
        "MCUBRIDGE_NON_INTERACTIVE": "1",
    }

    logger.info("Starting Daemon...")
    daemon_proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "mcubridge.daemon",
            "--debug",
            "--serial-port",
            SOCAT_PORT0,
            "--serial-baud",
            "115200",
            "--mqtt-host",
            MQTT_HOST,
            "--mqtt-port",
            str(MQTT_PORT),
            "--mqtt-tls",
            "0",
            "--serial-shared-secret",
            "DEBUG_INSECURE",
            "--allowed-commands",
            "*",
            "--non-interactive",
        ],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    _start_worker_thread(_daemon_worker, "daemon-worker", daemon_proc, state)

    # 4. Run Test
    all_success = True
    try:
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
                    subprocess.run(
                        [sys.executable, script], env=daemon_env, check=True, timeout=60
                    )
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
    typer.run(main)
