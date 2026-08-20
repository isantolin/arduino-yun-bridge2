#!/usr/bin/env python3
"""Hardware-accurate AVR CPU Emulation Runner using simavr.

Executes ATmega32u4 / ATmega328P / ATmega2560 ELF binaries compiled by avr-gcc
inside simavr, binding the virtual UART to the mcubridge Python daemon and
running full E2E client verification suites.
"""

from __future__ import annotations

import contextlib
import os
import pty
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root / "mcubridge") not in sys.path:
    sys.path.insert(0, str(repo_root / "mcubridge"))
if str(repo_root / "mcubridge-client-examples") not in sys.path:
    sys.path.insert(0, str(repo_root / "mcubridge-client-examples"))
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import structlog  # noqa: E402
import typer  # noqa: E402
from mcubridge.config.logging import configure_logging  # noqa: E402

configure_logging(console=True)
logger = structlog.get_logger("simavr-runner")

BOARD_TO_MCU: dict[str, str] = {
    "arduino:avr:yun": "atmega32u4",
    "arduino:avr:uno": "atmega328p",
    "arduino:avr:mega": "atmega2560",
    "arduino:avr:leonardo": "atmega32u4",
}


def _empty_output_lines() -> list[tuple[str, str]]:
    return []


@dataclass
class SimavrState:
    output_lines: list[tuple[str, str]] = field(default_factory=_empty_output_lines)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def on_line(self, line: str, source: str) -> None:
        clean_line = line.strip()
        if not clean_line:
            return
        with self.lock:
            self.output_lines.append((source, clean_line))
            logger.info("Process output", source=source, line=clean_line)


def _start_worker_thread(target: Any, name: str, *args: Any) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, args=args, daemon=True)
    thread.start()
    return thread


def _stderr_worker(proc: subprocess.Popen[bytes], state: SimavrState, source: str) -> None:
    if proc.stderr:
        for line in iter(proc.stderr.readline, b""):
            if not line:
                break
            try:
                decoded = line.decode("utf-8", errors="replace")
            except Exception:
                decoded = f"<hex:{line.hex()}>"
            state.on_line(decoded, source)


def _daemon_stdout_worker(proc: subprocess.Popen[str], state: SimavrState) -> None:
    if proc.stdout:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            state.on_line(line, "daemon")


def run_simavr_emulation(
    firmware_path: Path,
    mcu: str,
    frequency: int,
    test_scripts: list[Path],
    timeout_seconds: float = 60.0,
) -> bool:
    """Run full E2E tests against an AVR ELF running in simavr."""
    if not firmware_path.exists():
        logger.error("Firmware ELF not found", path=str(firmware_path))
        return False

    state = SimavrState()

    # Create virtual PTY for simavr UART
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    logger.info("Created virtual PTY for simavr", master=master_fd, pty=slave_name)

    simavr_cmd = [
        "simavr",
        "-m",
        mcu,
        "-f",
        str(frequency),
        str(firmware_path),
    ]

    logger.info("Spawning simavr process", cmd=simavr_cmd)
    simavr_proc = subprocess.Popen(
        simavr_cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    # Close slave in parent so EOF propagates when child exits
    os.close(slave_fd)

    _start_worker_thread(_stderr_worker, "simavr-stderr", simavr_proc, state, "simavr")

    with tempfile.TemporaryDirectory() as tmpdir:
        socket_path = Path(tmpdir) / "mcubridge.sock"
        daemon_env = dict(os.environ)
        daemon_env["MCUBRIDGE_SOCKET_PATH"] = str(socket_path)
        daemon_env["MCUBRIDGE_LOG_STREAM"] = "1"
        daemon_env["MCUBRIDGE_DEBUG"] = "1"

        daemon_cmd = [
            sys.executable,
            "-m",
            "mcubridge",
            "--port",
            slave_name,
            "--socket-path",
            str(socket_path),
            "--log-stream",
        ]

        logger.info("Spawning mcubridge daemon", cmd=daemon_cmd)
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            env=daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        _start_worker_thread(_daemon_stdout_worker, "daemon-stdout", daemon_proc, state)

        # Wait for daemon IPC socket to become ready
        deadline = time.monotonic() + 15.0
        socket_ready = False
        while time.monotonic() < deadline:
            if socket_path.exists():
                socket_ready = True
                break
            time.sleep(0.2)

        if not socket_ready:
            logger.error("Daemon socket failed to appear", socket_path=str(socket_path))
            simavr_proc.terminate()
            daemon_proc.terminate()
            os.close(master_fd)
            return False

        logger.info("Daemon socket ready, executing client test suite...")
        all_passed = True

        for test_path in test_scripts:
            if not test_path.exists():
                logger.warn("Test script not found, skipping", path=str(test_path))
                continue

            test_env = dict(os.environ)
            test_env["MCUBRIDGE_SOCKET_PATH"] = str(socket_path)

            logger.info("Running client test", script=test_path.name)
            test_res = subprocess.run(
                [sys.executable, str(test_path)],
                env=test_env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )

            if test_res.returncode != 0:
                logger.error(
                    "Test failed",
                    script=test_path.name,
                    code=test_res.returncode,
                    stdout=test_res.stdout,
                    stderr=test_res.stderr,
                )
                all_passed = False
                break
            logger.info("Test passed", script=test_path.name)

        # Teardown processes gracefully
        logger.info("Tearing down processes...")
        daemon_proc.send_signal(signal.SIGTERM)
        with contextlib.suppress(Exception):
            daemon_proc.wait(timeout=5)

        simavr_proc.send_signal(signal.SIGTERM)
        with contextlib.suppress(Exception):
            simavr_proc.wait(timeout=5)

        with contextlib.suppress(Exception):
            os.close(master_fd)

        return all_passed


app = typer.Typer(
    help="Cycle-accurate AVR hardware emulation using simavr.",
    add_completion=False,
)


@app.command()
def main(
    firmware: Annotated[
        Path,
        typer.Option(
            "--firmware",
            "-f",
            help="Path to AVR ELF firmware binary",
        ),
    ] = Path("build/simavr/arduino-avr-yun/firmware.elf"),
    board: Annotated[
        str,
        typer.Option(
            "--board",
            "-b",
            help="Arduino board FQBN or MCU name (e.g. arduino:avr:yun, atmega32u4, arduino:avr:uno)",
        ),
    ] = "arduino:avr:yun",
    frequency: Annotated[
        int,
        typer.Option(
            "--frequency",
            "-hz",
            help="AVR CPU clock frequency in Hz",
        ),
    ] = 16000000,
    tests: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Test scripts to execute against the emulated AVR bridge",
        ),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            "-t",
            help="Timeout per test script in seconds",
        ),
    ] = 30.0,
) -> None:
    """Run simavr AVR CPU emulation against mcubridge and client tests."""
    resolved_mcu = BOARD_TO_MCU.get(board, board)
    logger.info(
        "Starting simavr runner",
        board=board,
        mcu=resolved_mcu,
        frequency=frequency,
        firmware=str(firmware),
    )

    test_list = tests or [
        repo_root / "mcubridge-client-examples" / "mailbox_read_test.py",
        repo_root / "mcubridge-client-examples" / "sensor_reader_test.py",
        repo_root / "mcubridge-client-examples" / "console_test.py",
        repo_root / "mcubridge-client-examples" / "datastore_test.py",
        repo_root / "mcubridge-client-examples" / "fileio_test.py",
    ]

    success = run_simavr_emulation(
        firmware_path=firmware,
        mcu=resolved_mcu,
        frequency=frequency,
        test_scripts=test_list,
        timeout_seconds=timeout,
    )

    if not success:
        logger.error("simavr emulation suite failed!")
        sys.exit(1)
    logger.info("simavr emulation suite completed successfully!")


if __name__ == "__main__":
    app()
