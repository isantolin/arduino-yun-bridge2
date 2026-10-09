#!/usr/bin/env python3
"""Hardware-accurate AVR CPU Emulation Runner using simavr.

Executes ATmega32u4 / ATmega328P / ATmega2560 ELF binaries compiled by avr-gcc
inside simavr, binding the virtual UART to the mcubridge Python daemon and
running full E2E client verification suites.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import pty
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import structlog
import tenacity
import typer
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import protocol
from tools.arduino_core_metadata import read_metadata_json, resolve_core_metadata
from tools.emulation.process_utils import (
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
    wait_for_tcp_ready,
    write_fake_uci_module,
)

repo_root = Path(__file__).resolve().parents[2]

CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = protocol.DEFAULT_CLOUD_PORT

configure_logging(console=True)
logger = structlog.get_logger("simavr_runner")


def _read_pty_from_stream(proc_stdout: Any, state: SimavrState) -> str:
    """Read PTY line emitted by simavr harness with tenacity retry."""

    def _read_line() -> str | None:
        line = proc_stdout.readline()
        if line:
            state.on_line(line, "simavr-stdout")
            if "[SIMAVR] UART" in line and "PTY ready on:" in line:
                return line.split(":", 1)[1].strip()
        return None

    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_delay(10.0),
        wait=tenacity.wait_fixed(0.05),
        retry=tenacity.retry_if_result(lambda res: res is None),
        reraise=False,
    )
    try:
        detected = retryer(_read_line)
        return detected if isinstance(detected, str) else ""
    except tenacity.RetryError as exc:
        logger.warning("Timeout waiting for PTY ready line from simavr", error=str(exc))
        return ""


def _spawn_simavr(
    harness_bin: Path | None,
    firmware_path: Path,
    mcu: str,
    frequency: int,
    uart_id: str | None,
    state: SimavrState,
) -> tuple[subprocess.Popen[str] | None, str, int]:
    """Spawn simavr subprocess via harness or direct fallback."""
    master_fd = -1
    slave_name = ""
    simavr_proc: subprocess.Popen[str] | None = None

    if harness_bin and harness_bin.exists():
        simavr_cmd = [str(harness_bin), str(firmware_path), mcu, str(frequency)]
        if uart_id:
            simavr_cmd.append(uart_id)
        logger.info("Spawning simavr_harness", cmd=simavr_cmd)
        simavr_proc = subprocess.Popen(
            simavr_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        if simavr_proc.stdout is not None:
            slave_name = _read_pty_from_stream(simavr_proc.stdout, state)
        start_daemon_thread(stream_pump, "simavr-stdout", simavr_proc.stdout, state.on_line, "simavr-stdout")
        start_daemon_thread(stream_pump, "simavr-stderr", simavr_proc.stderr, state.on_line, "simavr-stderr")
    else:
        master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        logger.info("Created virtual PTY for simavr", master=master_fd, pty=slave_name)
        simavr_cmd = ["simavr", "-m", mcu, "-f", str(frequency), str(firmware_path)]
        logger.info("Spawning simavr process", cmd=simavr_cmd)
        try:
            simavr_proc = subprocess.Popen(
                simavr_cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                close_fds=True,
            )
            os.close(slave_fd)
            start_daemon_thread(stream_pump, "simavr-stderr", simavr_proc.stderr, state.on_line, "simavr-stderr")
        except FileNotFoundError:
            logger.error("simavr binary not found on system. Please install libsimavr-dev and simavr")
            os.close(slave_fd)
            if master_fd >= 0:
                os.close(master_fd)
            return None, "", -1

    return simavr_proc, slave_name, master_fd


def _ensure_cloud_gateway(state: SimavrState) -> subprocess.Popen[str] | None:
    """Start Managed Cloud Gateway for simavr if not already listening."""
    gateway_proc: subprocess.Popen[str] | None = None
    if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=1.0):
        logger.info("Starting Managed Cloud Gateway for simavr...")
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

    if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=30.0):
        logger.error("Cloud Gateway not available for simavr")
        if gateway_proc:
            terminate_process_tree([gateway_proc], timeout=1.0)
        return None

    return gateway_proc


def _setup_fake_uci(slave_name: str) -> tuple[Path, Path, dict[str, str]]:
    """Configure fake UCI directory, LMDB storage, and daemon environment variables."""
    fake_uci_dir = Path(tempfile.mkdtemp(prefix="mcubridge_simavr_uci_"))
    storage_path = Path(tempfile.mkdtemp(prefix="mcubridge_simavr_db_"))

    uci_config = {
        "serial_port": slave_name,
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "cloud_enabled": "1",
        "cloud_host": CLOUD_HOST,
        "cloud_port": str(CLOUD_PORT),
        "cloud_tls": "0",
        "cloud_tls_insecure": "1",
        "watchdog_enabled": "0",
        "serial_shared_secret": "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe",
        "allowed_commands": "*",
        "storage_path": str(storage_path),
        "debug": "1",
    }
    write_fake_uci_module(fake_uci_dir, uci_config, label="simavr runner")

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
    daemon_env["MCUBRIDGE_SERIAL_PORT"] = slave_name
    daemon_env["MCUBRIDGE_SERIAL_SAFE_BAUD"] = str(protocol.DEFAULT_SAFE_BAUDRATE)
    daemon_env["MCUBRIDGE_SERIAL_BAUD"] = str(protocol.DEFAULT_BAUDRATE)
    daemon_env["MCUBRIDGE_SERIAL_SHARED_SECRET"] = "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
    daemon_env["MCUBRIDGE_DISABLE_METRICS"] = "1"
    daemon_env["MCUBRIDGE_STORAGE_PATH"] = str(storage_path)
    daemon_env["MCUBRIDGE_CLOUD_ENABLED"] = "1"
    daemon_env["MCUBRIDGE_CLOUD_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_CLOUD_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_GATEWAY_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_GATEWAY_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_DEVICE_ID"] = "yun-01"

    return fake_uci_dir, storage_path, daemon_env


def default_client_test_paths() -> list[Path]:
    return [
        repo_root / "mcubridge-client-examples" / "tests" / "test_smoke_connection.py",
        repo_root / "mcubridge-client-examples" / "examples" / "led13_test.py",
        repo_root / "mcubridge-client-examples" / "examples" / "console_test.py",
        repo_root / "mcubridge-client-examples" / "examples" / "mailbox_read_test.py",
    ]


def _empty_output_lines() -> list[tuple[str, str]]:
    return []


@dataclass
class SimavrState:
    output_lines: list[tuple[str, str]] = field(default_factory=_empty_output_lines)
    lock: threading.Lock = field(default_factory=threading.Lock)
    sync_event: threading.Event = field(default_factory=threading.Event)
    capabilities_event: threading.Event = field(default_factory=threading.Event)

    def on_line(self, line: str, source: str) -> None:
        clean_line = line.strip()
        if not clean_line:
            return
        with self.lock:
            self.output_lines.append((source, clean_line))
            logger.info("Process output", source=source, line=clean_line)
            if "MCU capabilities received" in clean_line:
                self.capabilities_event.set()
                self.sync_event.set()
            elif (
                ('"event": "MCU ACK received"' in clean_line and '"command_id": "0x44"' in clean_line)
                or "MCU link synchronised" in clean_line
                or "Handshake synchronization complete" in clean_line
                or '"new_state": "SYNCHRONIZED"' in clean_line
            ):
                self.sync_event.set()


def _build_simavr_harness() -> Path | None:
    harness_src = repo_root / "tools" / "emulation" / "simavr_harness.cpp"
    harness_bin = repo_root / "build" / "simavr" / "simavr_harness"
    if not harness_src.exists():
        return None

    if harness_bin.exists() and harness_bin.stat().st_mtime >= harness_src.stat().st_mtime:
        return harness_bin

    harness_bin.parent.mkdir(parents=True, exist_ok=True)

    arduino_etl_include = Path.home() / "Arduino" / "libraries" / "Embedded_Template_Library" / "include"
    if not arduino_etl_include.exists():
        for candidate in [
            repo_root / ".dummy_libs" / "Embedded_Template_Library" / "include",
            Path.home() / ".local" / "include",
            Path("/usr/local/include"),
        ]:
            if candidate.exists():
                arduino_etl_include = candidate
                break

    compile_cmd = [
        "g++",
        "-std=c++17",
        "-O2",
        "-DETL_NO_STL",
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
        logger.warning("Failed to compile simavr_harness via g++", stderr=res.stderr)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("g++ not available to build simavr_harness", error=str(exc))
    return None


def run_simavr_emulation(
    firmware_path: Path,
    mcu: str,
    frequency: int,
    led_builtin_pin: int,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    uart_id: str | None = None,
) -> bool:
    """Run full E2E tests against an AVR ELF running in simavr."""
    if not firmware_path.exists():
        logger.error("Firmware ELF not found", path=str(firmware_path))
        return False

    state = SimavrState()
    harness_bin = _build_simavr_harness()

    simavr_proc, slave_name, master_fd = _spawn_simavr(harness_bin, firmware_path, mcu, frequency, uart_id, state)
    if not slave_name or not simavr_proc:
        logger.error("Failed to allocate virtual UART PTY device")
        if simavr_proc:
            simavr_proc.terminate()
        return False

    gateway_proc = _ensure_cloud_gateway(state)
    if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=1.0):
        _teardown_simavr(None, simavr_proc, gateway_proc, master_fd, None, None)
        return False

    fake_uci_dir, storage_path, daemon_env = _setup_fake_uci(slave_name)

    daemon_cmd = [
        sys.executable,
        "-u",
        "-m",
        "mcubridge.daemon",
    ]

    logger.info("Spawning mcubridge daemon", cmd=daemon_cmd)
    daemon_proc = subprocess.Popen(
        daemon_cmd,
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    start_daemon_thread(stream_pump, "daemon-stdout", daemon_proc.stdout, state.on_line, "daemon-stdout")
    start_daemon_thread(stream_pump, "daemon-stderr", daemon_proc.stderr, state.on_line, "daemon-stderr")

    # Ensure daemon process started cleanly
    time.sleep(0.5)
    if daemon_proc.poll() is not None:
        logger.error("Daemon exited prematurely", returncode=daemon_proc.returncode)
        _teardown_simavr(daemon_proc, simavr_proc, gateway_proc, master_fd, fake_uci_dir, storage_path)
        return False

    # Allow daemon and MCU to complete cryptographic handshake
    logger.info("Waiting for MCU/daemon link cryptographic synchronization...")
    if not state.sync_event.wait(timeout=60.0):
        _teardown_simavr(daemon_proc, simavr_proc, gateway_proc, master_fd, fake_uci_dir, storage_path)
        return False

    logger.info("MCU/daemon link synchronized successfully! Waiting for post-handshake capabilities...")
    # Wait for capabilities discovery to complete so in-flight frames don't collide with client tests
    state.capabilities_event.wait(timeout=5.0)
    time.sleep(1.0)

    all_passed = run_client_scripts(test_scripts, daemon_env, timeout_seconds, led_builtin_pin=led_builtin_pin)
    if all_passed:
        # [SIL-2 / Rule 29] Audit runtime status snapshot before teardown
        status_file = Path("/tmp/mcubridge_status.json")
        if status_file.exists():
            try:
                from tools.audit.audit_bridge_status import audit_status_dict

                status_errors = audit_status_dict(json.loads(status_file.read_text(encoding="utf-8")))
                if status_errors:
                    logger.error("Post-execution status health check failed", errors=status_errors)
                    all_passed = False
                else:
                    logger.info("Post-execution status health check passed (100% clean)")
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.error("Failed auditing bridge status", error=str(exc))
                all_passed = False

    _teardown_simavr(daemon_proc, simavr_proc, gateway_proc, master_fd, fake_uci_dir, storage_path)
    return all_passed


def run_single_client_script(
    script_path: Path,
    device_id: str = "yun-01",
    led_builtin_pin: int | None = None,
) -> bool:
    """Execute client test script directly in-process via module main or runpy (Rule 37)."""
    logger.info("Running client test in-process", script=script_path.name)
    try:
        spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            main_fn = getattr(mod, "main", None)
            if callable(main_fn):
                main_parameters = inspect.signature(main_fn).parameters
                kwargs: dict[str, object] = {
                    "host": CLOUD_HOST,
                    "port": CLOUD_PORT,
                    "device_id": device_id,
                }
                if "led_builtin_pin" in main_parameters:
                    if led_builtin_pin is None:
                        raise ValueError(f"{script_path.name} requires LED_BUILTIN from its Arduino core")
                    kwargs["led_builtin_pin"] = led_builtin_pin
                main_fn(**kwargs)
                logger.info("Test passed", script=script_path.name)
                return True

        orig_argv = sys.argv[:]
        try:
            sys.argv = [str(script_path), "--device-id", device_id]
            runpy.run_path(str(script_path), run_name="__main__")
        finally:
            sys.argv = orig_argv

        logger.info("Test passed", script=script_path.name)
        return True
    except SystemExit as exc:
        if exc.code in (0, None):
            logger.info("Test passed", script=script_path.name)
            return True
        logger.error("Test failed with exit code", script=script_path.name, code=exc.code)
        return False
    except (OSError, RuntimeError, ValueError, TypeError, AssertionError, TimeoutError) as exc:
        logger.error("Test failed with exception", script=script_path.name, error=str(exc))
        return False


def run_client_scripts(
    test_scripts: list[Path],
    daemon_env: dict[str, str],
    timeout_seconds: float,
    led_builtin_pin: int,
) -> bool:
    _ = (daemon_env, timeout_seconds)
    for test_path in test_scripts:
        if not test_path.exists():
            logger.warning("Test script not found, skipping", path=str(test_path))
            continue
        if not run_single_client_script(test_path, device_id="yun-01", led_builtin_pin=led_builtin_pin):
            return False
    return True


def _teardown_simavr(
    daemon_proc: subprocess.Popen[Any] | None,
    simavr_proc: subprocess.Popen[Any] | None,
    gateway_proc: subprocess.Popen[Any] | None,
    master_fd: int,
    fake_uci_dir: Path | str | None = None,
    storage_path: Path | str | None = None,
) -> None:
    procs = [p for p in (daemon_proc, simavr_proc, gateway_proc) if p is not None]
    terminate_process_tree(procs, timeout=5.0)

    if master_fd >= 0:
        try:
            os.close(master_fd)
        except OSError as exc:
            logger.debug("Failed closing master_fd during teardown", error=str(exc))

    if fake_uci_dir is not None:
        fake_uci_path = Path(fake_uci_dir)
        if fake_uci_path.exists():
            shutil.rmtree(fake_uci_path)
    if storage_path is not None:
        storage_p = Path(storage_path)
        if storage_p.exists():
            shutil.rmtree(storage_p)


MATRIX_BOARDS: list[tuple[str, str]] = [
    ("arduino:avr:mega", "Arduino Mega 2560 (ATmega2560)"),
]


def run_matrix(
    sketch_path: Path,
    timeout_seconds: float = 90.0,
    test_scripts: list[Path] | None = None,
) -> int:
    """Execute multi-board simavr emulation matrix natively in Python (Rule 37)."""
    build_base_dir = repo_root / "build" / "simavr"
    summary_dir_str = os.environ.get("SIMAVR_METRICS_DIR")
    summary_dir = Path(summary_dir_str) if summary_dir_str else build_base_dir
    summary_dir.mkdir(parents=True, exist_ok=True)
    build_base_dir.mkdir(parents=True, exist_ok=True)

    test_paths = default_client_test_paths() if test_scripts is None else test_scripts

    compile_script = repo_root / "tools" / "ci" / "compile_simavr_firmware.sh"

    compilation_status: list[str] = []
    emulation_status: list[str] = []
    fail_count = 0

    for board_fqbn, board_name in MATRIX_BOARDS:
        slug = board_fqbn.replace(":", "-")
        out_dir = build_base_dir / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        firmware_elf = out_dir / "firmware.elf"
        firmware_elf.unlink(missing_ok=True)

        logger.info("=" * 80)
        logger.info("Testing Board in simavr matrix", board=board_fqbn, name=board_name)
        logger.info("=" * 80)

        compile_res = subprocess.run(
            ["bash", str(compile_script), str(sketch_path), board_fqbn, str(out_dir)],
            check=False,
        )

        if compile_res.returncode == 0 and firmware_elf.exists():
            compilation_status.append("✅ Compiled")
            metadata = read_metadata_json(out_dir / "arduino_core_metadata.json")
            if metadata.fqbn != board_fqbn:
                raise RuntimeError(f"Firmware for {board_fqbn} has metadata for {metadata.fqbn}")
            uart_id: str | None = None
            sketch_str = str(sketch_path)
            if ("BridgeBluetooth" in sketch_str or "BridgeWiFi" in sketch_str) and board_fqbn == "arduino:avr:mega":
                uart_id = "1"

            success = run_simavr_emulation(
                firmware_path=firmware_elf,
                mcu=metadata.mcu,
                frequency=metadata.frequency_hz,
                led_builtin_pin=metadata.led_builtin,
                test_scripts=test_paths,
                timeout_seconds=timeout_seconds,
                uart_id=uart_id,
            )
            if success:
                logger.info("Emulation PASSED for board", board=board_fqbn)
                emulation_status.append("✅ Passed (100% E2E)")
            else:
                logger.error("Emulation FAILED for board", board=board_fqbn)
                emulation_status.append("❌ Failed")
                fail_count += 1
        else:
            compilation_status.append("❌ Failed")
            emulation_status.append("⏭️ Not run (firmware unavailable)")
            fail_count += 1

    summary_file = summary_dir / "simavr_summary.md"
    rows: list[str] = [
        "### 🔬 simavr AVR Hardware Emulation Matrix (Cycle-Accurate)\n",
        "| Board / Target | MCU Architecture | Firmware Compilation | Hardware Emulation (PTY/UART) | Result |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]

    for (board_fqbn, board_name), c_stat, e_stat in zip(
        MATRIX_BOARDS, compilation_status, emulation_status, strict=True
    ):
        if "Passed" in e_stat:
            overall = "✅ PASS"
        elif "Skipped" in e_stat:
            overall = "⏭️ SKIPPED"
        else:
            overall = "❌ FAIL"
        rows.append(f"| **{board_name}**<br>`{board_fqbn}` | AVR 8-bit | {c_stat} | {e_stat} | **{overall}** |")

    summary_content = "\n".join(rows) + "\n"
    summary_file.write_text(summary_content, encoding="utf-8")
    print(summary_content)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with Path(step_summary).open("a", encoding="utf-8") as f:
                f.write(summary_content)
        except OSError as exc:
            logger.warning("Could not write to GITHUB_STEP_SUMMARY", error=str(exc))

    simavr_logs_dir = repo_root / "simavr-logs"
    if simavr_logs_dir.exists():
        shutil.copy2(summary_file, simavr_logs_dir / "simavr_summary.md")

    return fail_count


app = typer.Typer(
    help="Cycle-accurate AVR hardware emulation using simavr.",
    add_completion=False,
)


@app.command()
def main(
    firmware: Annotated[
        Path | None,
        typer.Option(
            "--firmware",
            "-f",
            help="Path to AVR ELF firmware binary",
        ),
    ] = None,
    board: Annotated[
        str,
        typer.Option(
            "--board",
            "-b",
            help="AVR board FQBN or MCU name (e.g. arduino:avr:mega, atmega2560)",
        ),
    ] = "arduino:avr:mega",
    frequency: Annotated[
        int | None,
        typer.Option(
            "--frequency",
            "-F",
            help="Explicit AVR CPU clock override; otherwise use the selected Arduino core's F_CPU",
        ),
    ] = None,
    led_builtin_pin: Annotated[
        int | None,
        typer.Option("--led-builtin-pin", help="Explicit LED_BUILTIN pin for raw MCU targets"),
    ] = None,
    sketch: Annotated[
        Path | None,
        typer.Option(
            "--sketch",
            "-s",
            help="Path to Arduino sketch (.ino) to compile and emulate",
        ),
    ] = None,
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
    ] = 90.0,
    uart: Annotated[
        str | None,
        typer.Option(
            "--uart",
            "-u",
            help="UART peripheral ID (e.g. 0, 1, or auto)",
        ),
    ] = None,
) -> None:
    """Entrypoint for the simavr hardware emulation runner."""
    if sketch is not None:
        if frequency is not None or led_builtin_pin is not None:
            raise typer.BadParameter("Sketch matrix runs always use metadata from each target Arduino core")
        effective_sketch = sketch
        if not effective_sketch.exists():
            raise typer.BadParameter(f"Sketch does not exist: {effective_sketch}")

        test_paths = [Path(s) for s in scripts] if scripts else None
        fail_count = run_matrix(effective_sketch, timeout_seconds=timeout, test_scripts=test_paths)
        if fail_count != 0:
            sys.exit(fail_count)
        return

    if frequency is not None and frequency <= 0:
        raise typer.BadParameter("frequency must be greater than zero")

    metadata = resolve_core_metadata(board) if ":" in board else None
    if metadata is None:
        if frequency is None or led_builtin_pin is None:
            raise typer.BadParameter("Raw MCU targets require both --frequency and --led-builtin-pin")
        effective_frequency = frequency
        effective_led_pin = led_builtin_pin
    else:
        effective_frequency = frequency if frequency is not None else metadata.frequency_hz
        effective_led_pin = led_builtin_pin if led_builtin_pin is not None else metadata.led_builtin

    effective_firmware = firmware or Path(f"build/simavr/{board.replace(':', '-')}/firmware.elf")
    mcu = metadata.mcu if metadata is not None else board.lower()

    # Auto-detect UART ID based on firmware name if not explicitly specified
    effective_uart = uart
    if not effective_uart:
        fw_str = str(effective_firmware).lower()
        if ("bluetooth" in fw_str or "wifi" in fw_str) and mcu == "atmega2560" or mcu == "atmega32u4":
            effective_uart = "1"
        else:
            effective_uart = "0"

    test_paths = [Path(s) for s in scripts] if scripts else default_client_test_paths()

    logger.info(
        "Starting simavr runner",
        board=board,
        mcu=mcu,
        frequency=effective_frequency,
        firmware=str(effective_firmware),
        uart=effective_uart,
    )

    success = run_simavr_emulation(
        firmware_path=effective_firmware,
        mcu=mcu,
        frequency=effective_frequency,
        led_builtin_pin=effective_led_pin,
        test_scripts=test_paths,
        timeout_seconds=timeout,
        uart_id=effective_uart,
    )

    if not success:
        logger.error("simavr emulation suite failed!")
        sys.exit(1)

    logger.info("All simavr emulation tests completed successfully!")


if __name__ == "__main__":
    app()
