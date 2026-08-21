#!/usr/bin/env python3
"""Hardware-accurate AVR CPU Emulation Runner using simavr.

Executes ATmega32u4 / ATmega328P / ATmega2560 ELF binaries compiled by avr-gcc
inside simavr, binding the virtual UART to the mcubridge Python daemon and
running full E2E client verification suites.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import shutil
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
    sync_event: threading.Event = field(default_factory=threading.Event)

    def on_line(self, line: str, source: str) -> None:
        clean_line = line.strip()
        if not clean_line:
            return
        with self.lock:
            self.output_lines.append((source, clean_line))
            logger.info("Process output", source=source, line=clean_line)
            if '"event": "MCU ACK received"' in clean_line and '"command_id": "0x44"' in clean_line:
                self.sync_event.set()
            elif "MCU link synchronised" in clean_line:
                self.sync_event.set()
            elif "Handshake synchronization complete" in clean_line:
                self.sync_event.set()
            elif '"new_state": "SYNCHRONIZED"' in clean_line:
                self.sync_event.set()


def _start_worker_thread(target: Any, name: str, *args: Any) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, args=args, daemon=True)
    thread.start()
    return thread


def _stream_worker(stream: Any, state: SimavrState, source: str) -> None:
    if stream:
        for line in iter(stream.readline, ""):
            if not line:
                break
            state.on_line(str(line), source)


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


def _build_simavr_harness() -> Path | None:
    harness_src = repo_root / "tools" / "simavr_harness.cpp"
    harness_bin = repo_root / "build" / "simavr" / "simavr_harness"
    if not harness_src.exists():
        return None

    if harness_bin.exists() and harness_bin.stat().st_mtime >= harness_src.stat().st_mtime:
        return harness_bin

    harness_bin.parent.mkdir(parents=True, exist_ok=True)

    etl_include = repo_root / ".dummy_libs" / "Embedded_Template_Library" / "include"
    arduino_etl_include = Path.home() / "Arduino" / "libraries" / "Embedded_Template_Library" / "include"

    compile_cmd = [
        "g++",
        "-std=c++17",
        "-O2",
        "-DETL_NO_STL",
        "-I",
        str(etl_include),
        "-I",
        str(arduino_etl_include),
        str(harness_src),
        "-lsimavr",
        "-lutil",
        "-o",
        str(harness_bin),
    ]
    try:
        res = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and harness_bin.exists():
            logger.info("Compiled ETL-compliant simavr_harness binary", binary=str(harness_bin))
            return harness_bin
        logger.warn("Failed to compile simavr_harness via g++", stderr=res.stderr)
    except Exception as exc:
        logger.warn("g++ not available to build simavr_harness", error=str(exc))
    return None


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
    harness_bin = _build_simavr_harness()

    master_fd = -1
    slave_name = ""
    simavr_proc = None

    if harness_bin and harness_bin.exists():
        simavr_cmd = [str(harness_bin), str(firmware_path), mcu, str(frequency)]
        logger.info("Spawning simavr_harness", cmd=simavr_cmd)
        simavr_proc = subprocess.Popen(
            simavr_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
        )
        # Parse PTY path emitted by harness
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and simavr_proc.stdout:
            line = simavr_proc.stdout.readline()
            if line:
                state.on_line(line, "simavr-stdout")
                if "[SIMAVR] UART PTY ready on:" in line:
                    slave_name = line.split(":", 1)[1].strip()
                    break
            time.sleep(0.05)
        _start_worker_thread(_stream_worker, "simavr-stdout", simavr_proc.stdout, state, "simavr-stdout")
        _start_worker_thread(_stream_worker, "simavr-stderr", simavr_proc.stderr, state, "simavr-stderr")
    else:
        # Fallback to socat / pty.openpty()
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
        try:
            simavr_proc = subprocess.Popen(
                simavr_cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
                close_fds=True,
            )
            os.close(slave_fd)
            _start_worker_thread(_stream_worker, "simavr-stderr", simavr_proc.stderr, state, "simavr-stderr")
        except FileNotFoundError:
            logger.error("simavr binary not found on system. Please install libsimavr-dev and simavr")
            os.close(slave_fd)
            if master_fd >= 0:
                os.close(master_fd)
            return False

    if not slave_name:
        logger.error("Failed to allocate virtual UART PTY device")
        if simavr_proc:
            simavr_proc.terminate()
        return False

    fake_uci_dir = Path(tempfile.mkdtemp(prefix="mcubridge_simavr_uci_"))
    socket_path = fake_uci_dir / "mcubridge.sock"
    storage_path = Path(tempfile.mkdtemp(prefix="mcubridge_simavr_db_"))

    uci_config = {
        "serial_port": slave_name,
        "serial_baud": "115200",
        "serial_safe_baud": "115200",
        "cloud_enabled": "0",
        "metrics_enabled": "0",
        "watchdog_enabled": "0",
        "serial_shared_secret": "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe",
        "allowed_commands": "*",
        "storage_path": str(storage_path),
        "debug": "1",
    }
    _write_fake_uci_module(fake_uci_dir, uci_config)

    daemon_env = dict(os.environ)
    extra_paths = [
        str(fake_uci_dir),
        str(repo_root / "mcubridge"),
        str(repo_root / "mcubridge-client-examples"),
        str(repo_root),
    ]
    curr_pp = daemon_env.get("PYTHONPATH", "")
    daemon_env["PYTHONPATH"] = ":".join(extra_paths + ([curr_pp] if curr_pp else []))
    daemon_env["PYTHONUNBUFFERED"] = "1"
    daemon_env["MCUBRIDGE_FORCE_UCI"] = "1"
    daemon_env["MCUBRIDGE_NON_INTERACTIVE"] = "1"
    daemon_env["MCUBRIDGE_LOG_STREAM"] = "1"
    daemon_env["MCUBRIDGE_SOCKET_PATH"] = str(socket_path)
    daemon_env["MCUBRIDGE_SERIAL_PORT"] = slave_name
    daemon_env["MCUBRIDGE_SERIAL_SAFE_BAUD"] = "115200"
    daemon_env["MCUBRIDGE_SERIAL_BAUD"] = "115200"
    daemon_env["MCUBRIDGE_SERIAL_SHARED_SECRET"] = "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
    daemon_env["MCUBRIDGE_DISABLE_METRICS"] = "1"
    daemon_env["MCUBRIDGE_STORAGE_PATH"] = str(storage_path)

    daemon_cmd = [
        sys.executable,
        "-m",
        "mcubridge.daemon",
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
        errors="replace",
        bufsize=1,
    )

    _start_worker_thread(_stream_worker, "daemon-stdout", daemon_proc.stdout, state, "daemon-stdout")
    _start_worker_thread(_stream_worker, "daemon-stderr", daemon_proc.stderr, state, "daemon-stderr")

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
        if daemon_proc.poll() is not None:
            logger.error("Daemon exited prematurely", returncode=daemon_proc.returncode)
        simavr_proc.terminate()
        daemon_proc.terminate()
        if master_fd >= 0:
            with contextlib.suppress(Exception):
                os.close(master_fd)
        shutil.rmtree(fake_uci_dir, ignore_errors=True)
        shutil.rmtree(storage_path, ignore_errors=True)
        return False

    # Allow daemon and MCU to complete cryptographic handshake
    logger.info("Waiting for MCU/daemon link cryptographic synchronization...")
    if not state.sync_event.wait(timeout=60.0):
        daemon_proc.terminate()
        if simavr_proc:
            simavr_proc.terminate()
        if master_fd >= 0:
            with contextlib.suppress(Exception):
                os.close(master_fd)
        shutil.rmtree(fake_uci_dir, ignore_errors=True)
        shutil.rmtree(storage_path, ignore_errors=True)
        return False

    logger.info("MCU/daemon link synchronized successfully!")
    time.sleep(1.0)

    logger.info("Executing client test suite on synchronized link...")
    all_passed = True

    for test_path in test_scripts:
        if not test_path.exists():
            logger.warn("Test script not found, skipping", path=str(test_path))
            continue

        test_env = dict(daemon_env)
        test_env["MCUBRIDGE_SOCKET_PATH"] = str(socket_path)

        logger.info("Running client test", script=test_path.name)
        test_res = subprocess.run(
            [sys.executable, str(test_path)],
            env=test_env,
            capture_output=True,
            text=True,
            errors="replace",
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

    if master_fd >= 0:
        with contextlib.suppress(Exception):
            os.close(master_fd)

    shutil.rmtree(fake_uci_dir, ignore_errors=True)
    shutil.rmtree(storage_path, ignore_errors=True)
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
    ] = Path("build/simavr/arduino-avr-mega/firmware.elf"),
    board: Annotated[
        str,
        typer.Option(
            "--board",
            "-b",
            help="Arduino board FQBN or MCU name (e.g. arduino:avr:mega, atmega2560, arduino:avr:yun)",
        ),
    ] = "arduino:avr:mega",
    frequency: Annotated[
        int,
        typer.Option(
            "--frequency",
            "-F",
            help="AVR CPU clock frequency in Hz (default: 16MHz)",
        ),
    ] = 16000000,
    scripts: Annotated[
        list[str] | None,
        typer.Argument(
            help="Test scripts to run (default: runs standard client smoke tests)",
        ),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            "-t",
            help="Timeout per client script in seconds",
        ),
    ] = 60.0,
) -> None:
    """Entrypoint for the simavr hardware emulation runner."""
    mcu = BOARD_TO_MCU.get(board.lower(), board.lower())

    if scripts:
        test_paths = [Path(s) for s in scripts]
    else:
        test_paths = [
            repo_root / "mcubridge-client-examples" / "client_tests" / "test_smoke_connection.py",
            repo_root / "mcubridge-client-examples" / "led13_test.py",
            repo_root / "mcubridge-client-examples" / "console_test.py",
            repo_root / "mcubridge-client-examples" / "mailbox_read_test.py",
        ]

    logger.info(
        "Starting simavr runner",
        board=board,
        mcu=mcu,
        frequency=frequency,
        firmware=str(firmware),
    )

    success = run_simavr_emulation(
        firmware_path=firmware,
        mcu=mcu,
        frequency=frequency,
        test_scripts=test_paths,
        timeout_seconds=timeout,
    )

    if not success:
        logger.error("simavr emulation suite failed!")
        sys.exit(1)

    logger.info("All simavr emulation tests completed successfully!")


if __name__ == "__main__":
    app()
