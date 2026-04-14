#!/usr/bin/env python3
"""Runner for end-to-end emulation tests using socat and BridgeDaemon."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer

from mcubridge.daemon import BridgeDaemon
from mcubridge.config.settings import load_runtime_config

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("emulation-runner")

app = typer.Typer()


def _wait_for_serial(path: str, timeout: float = 10.0) -> bool:
    """Wait for the serial port symlink to be created by socat."""
    start = time.time()
    while time.time() - start < timeout:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


async def run_daemon(config_dir: str) -> None:
    """Initialize and run the BridgeDaemon in-process."""
    # Ensure UCI configuration is picked up from the temporary directory
    os.environ["UCI_CONFIG_DIR"] = config_dir
    config = load_runtime_config()
    daemon = BridgeDaemon(config)
    await daemon.run()


@app.command()
def main(
    firmware: str = typer.Option(..., help="Path to the MCU firmware binary/emulator"),
    scripts: list[str] = typer.Argument(..., help="Test scripts to run against the bridge"),
    port: str = typer.Option("/tmp/ttyMCU", help="Virtual serial port path"),
) -> None:
    """Orchestrate the E2E emulation."""
    all_success = True

    # 1. Start socat to create virtual serial pair
    # PTY (for daemon) <-> PTY (for MCU emulator)
    logger.info("Starting socat for virtual serial link...")
    socat_proc = subprocess.Popen(
        [
            "socat",
            "-d",
            "-d",
            f"PTY,link={port},raw,echo=0",
            "PTY,link={port}_mcu,raw,echo=0",
        ]
    )

    if not _wait_for_serial(port):
        logger.error("Timed out waiting for serial port %s", port)
        socat_proc.terminate()
        sys.exit(1)

    # 2. Start MCU Emulator
    logger.info("Starting MCU emulator with firmware: %s", firmware)
    # [SECURITY] Use start_new_session instead of preexec_fn for thread safety
    mcu_proc = subprocess.Popen([firmware, f"{port}_mcu"], start_new_session=True)

    # 3. Start BridgeDaemon in background
    logger.info("Starting BridgeDaemon...")
    # We run the daemon in a separate process to avoid blocking and ensure isolation
    daemon_env = os.environ.copy()
    daemon_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "mcubridge")
    daemon_proc = subprocess.Popen(
        [sys.executable, "-m", "mcubridge.daemon", "--debug"],
        env=daemon_env,
    )

    # Allow some time for startup
    time.sleep(2)

    import shlex
    try:
        # 4. Execute Test Scripts
        for script_cmd in scripts:
            args = shlex.split(script_cmd)
            script_path = args[0]
            if not os.path.exists(script_path):
                logger.error("Script not found: %s", script_path)
                all_success = False
                continue

            logger.info("Running test script: %s", script_cmd)
            try:
                subprocess.run(
                    [sys.executable] + args, env=daemon_env, check=True, timeout=60
                )
                logger.info("Script %s PASSED.", script_path)
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ) as exc:
                logger.error("Script %s FAILED: %s", script_path, exc)
                all_success = False
                break

            # Small cool-down between scripts to keep logs separated
            time.sleep(1)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        logger.error("Emulation error: %s", exc)
        all_success = False
    finally:
        # Terminate daemon (same process group — plain kill only)
        for p in [daemon_proc]:
            try:
                os.kill(p.pid, signal.SIGTERM)
                p.wait(timeout=2)
            except (ProcessLookupError, OSError):
                pass
            try:
                os.kill(p.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        # Terminate socat+MCU (separate session — use process group)
        for p in [mcu_proc, socat_proc]:
            try:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
                p.wait(timeout=2)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

    if not all_success:
        logger.error("Emulation FAILED.")
        sys.exit(1)

    logger.info("Emulation SUCCESS.")


if __name__ == "__main__":
    app()
