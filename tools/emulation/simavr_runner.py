#!/usr/bin/env python3
"""Hardware-accurate AVR CPU Emulation Runner using simavr.

Executes ATmega32u4 / ATmega328P / ATmega2560 ELF binaries compiled by avr-gcc
inside simavr, binding the virtual UART to the mcubridge Python daemon and
running full E2E client verification suites.
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import parse
import structlog
import tenacity
import typer
from mcubridge.config.logging import configure_logging
from tools.arduino_core_metadata import read_metadata_json, resolve_core_metadata
from tools.boards import SIMAVR_BOARDS as MATRIX_BOARDS
from tools.emulation.emulation_runner import (
    CLOUD_HOST,
    CLOUD_PORT,
    EmulationState,
    audit_post_execution_status,
    default_client_test_paths,
    ensure_cloud_gateway,
    run_client_scripts,
    setup_fake_uci,
    write_matrix_summary,
)
from tools.emulation.process_utils import (
    cleanup_paths,
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
    wait_for_tcp_ready,
)

repo_root = Path(__file__).resolve().parents[2]

configure_logging(console=True)
logger = structlog.get_logger("simavr_runner")

_PTY_PARSER = parse.compile("[SIMAVR] UART{id:w} PTY ready on: {pty}")


def _read_pty_from_stream(proc_stdout: Any, state: EmulationState) -> str:
    """Read PTY line emitted by simavr harness with tenacity retry."""

    def _read_line() -> str | None:
        line = proc_stdout.readline()
        if line:
            state.on_line(line, "simavr-stdout")
            res = _PTY_PARSER.parse(line.strip())
            if res:
                return str(res["pty"])
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


def spawn_simavr(
    harness_bin: Path | None,
    firmware_path: Path,
    mcu: str,
    frequency: int,
    uart_id: str | None,
    state: EmulationState,
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


def build_simavr_harness() -> Path | None:
    harness_src = repo_root / "tools" / "emulation" / "simavr_harness.cpp"
    harness_bin = repo_root / "build" / "simavr" / "simavr_harness"
    if not harness_src.exists():
        return None

    if harness_bin.exists() and harness_bin.stat().st_mtime >= harness_src.stat().st_mtime:
        return harness_bin

    harness_bin.parent.mkdir(parents=True, exist_ok=True)

    arduino_etl_include: Path | None = None
    search_roots = [
        Path.home() / "Arduino" / "libraries",
        Path("/home/runner/Arduino/libraries"),
        repo_root / ".dummy_libs",
        Path.home() / ".local" / "include",
        Path("/usr/local/include"),
        Path("/usr/include"),
    ]

    for root in search_roots:
        if not root.is_dir():
            continue
        if (root / "etl" / "algorithm.h").is_file():
            arduino_etl_include = root
            break
        for match in root.rglob("etl/algorithm.h"):
            candidate_dir = match.parent.parent
            if (candidate_dir / "etl" / "algorithm.h").is_file():
                arduino_etl_include = candidate_dir
                break
        if arduino_etl_include is not None:
            break

    if arduino_etl_include is None:
        arduino_etl_include = Path.home() / "Arduino" / "libraries" / "Embedded_Template_Library" / "include"

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

    cleanup_paths(fake_uci_dir, storage_path)


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

    state = EmulationState()
    harness_bin = build_simavr_harness()

    gateway_proc = ensure_cloud_gateway(state)
    if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=1.0):
        _teardown_simavr(None, None, gateway_proc, -1, None, None)
        return False

    simavr_proc, slave_name, master_fd = spawn_simavr(harness_bin, firmware_path, mcu, frequency, uart_id, state)
    if not slave_name or not simavr_proc:
        logger.error("Failed to allocate virtual UART PTY device")
        _teardown_simavr(None, simavr_proc, gateway_proc, master_fd, None, None)
        return False

    fake_uci_dir, storage_path, daemon_env = setup_fake_uci(slave_name, device_id="yun-01", label="simavr runner")

    daemon_cmd = [sys.executable, "-u", "-m", "mcubridge.daemon"]
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

    time.sleep(0.5)
    if daemon_proc.poll() is not None:
        logger.error("Daemon exited prematurely", returncode=daemon_proc.returncode)
        _teardown_simavr(daemon_proc, simavr_proc, gateway_proc, master_fd, fake_uci_dir, storage_path)
        return False

    logger.info("Waiting for MCU/daemon link cryptographic synchronization...")
    if not state.sync_event.wait(timeout=max(timeout_seconds, 60.0)):
        _teardown_simavr(daemon_proc, simavr_proc, gateway_proc, master_fd, fake_uci_dir, storage_path)
        return False

    logger.info("MCU/daemon link synchronized successfully! Waiting for post-handshake capabilities...")
    state.capabilities_event.wait(timeout=30.0)
    time.sleep(1.0)

    all_passed = run_client_scripts(test_scripts, led_builtin_pin=led_builtin_pin) and audit_post_execution_status()
    _teardown_simavr(daemon_proc, simavr_proc, gateway_proc, master_fd, fake_uci_dir, storage_path)
    return all_passed


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
        elif compile_res.returncode == 0:
            compilation_status.append("⚠️ Skipped (Flash/RAM limit exceeded)")
            emulation_status.append("⏭️ Skipped (No binary)")
            logger.info("Emulation skipped (target memory limit exceeded)", board=board_fqbn)
        else:
            compilation_status.append("❌ Failed")
            emulation_status.append("⏭️ Not run (compilation failed)")
            fail_count += 1

    write_matrix_summary(
        summary_dir,
        prefix="simavr",
        title="simavr AVR Hardware Emulation Matrix (Cycle-Accurate)",
        engine_label="simavr (Cycle-Accurate)",
        architecture="AVR 8-bit",
        boards=MATRIX_BOARDS,
        compilation_status=compilation_status,
        emulation_status=emulation_status,
        mirror_dir=repo_root / "simavr-logs",
    )
    return fail_count


app = typer.Typer(
    help="Cycle-accurate AVR hardware emulation using simavr.",
    add_completion=False,
)


def _resolve_board_target(
    board: str,
    firmware: Path | None,
    frequency: int | None,
    led_builtin_pin: int | None,
    uart: str | None,
) -> tuple[Path, str, int, int, str]:
    if frequency is not None and frequency <= 0:
        raise typer.BadParameter("frequency must be greater than zero")

    metadata = resolve_core_metadata(board) if ":" in board else None
    if metadata is None:
        if frequency is None or led_builtin_pin is None:
            raise typer.BadParameter("Raw MCU targets require both --frequency and --led-builtin-pin")
        effective_frequency, effective_led_pin, mcu = frequency, led_builtin_pin, board.lower()
    else:
        effective_frequency = frequency if frequency is not None else metadata.frequency_hz
        effective_led_pin = led_builtin_pin if led_builtin_pin is not None else metadata.led_builtin
        mcu = metadata.mcu

    effective_firmware = firmware or Path(f"build/simavr/{board.replace(':', '-')}/firmware.elf")
    if uart:
        effective_uart = uart
    else:
        fw_str = str(effective_firmware).lower()
        uses_uart1 = mcu == "atmega32u4" or (mcu == "atmega2560" and ("bluetooth" in fw_str or "wifi" in fw_str))
        effective_uart = "1" if uses_uart1 else "0"
    return effective_firmware, mcu, effective_frequency, effective_led_pin, effective_uart


@app.command()
def main(
    firmware: Annotated[
        Path | None,
        typer.Option("--firmware", "-f", help="Path to AVR ELF firmware binary"),
    ] = None,
    board: Annotated[
        str,
        typer.Option("--board", "-b", help="AVR board FQBN or MCU name (e.g. arduino:avr:mega, atmega2560)"),
    ] = "arduino:avr:mega",
    frequency: Annotated[
        int | None,
        typer.Option("--frequency", "-F", help="Explicit AVR CPU clock override; otherwise use core F_CPU"),
    ] = None,
    led_builtin_pin: Annotated[
        int | None,
        typer.Option("--led-builtin-pin", help="Explicit LED_BUILTIN pin for raw MCU targets"),
    ] = None,
    sketch: Annotated[
        Path | None,
        typer.Option("--sketch", "-s", help="Path to Arduino sketch (.ino) to compile and emulate"),
    ] = None,
    scripts: Annotated[
        list[str] | None,
        typer.Argument(help="Test scripts to run (default: runs standard client smoke tests)"),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option("--timeout", "-t", help="Timeout per client script in seconds"),
    ] = 90.0,
    uart: Annotated[
        str | None,
        typer.Option("--uart", "-u", help="UART peripheral ID (e.g. 0, 1, or auto)"),
    ] = None,
) -> None:
    """Entrypoint for the simavr hardware emulation runner."""
    if sketch is not None:
        if frequency is not None or led_builtin_pin is not None:
            raise typer.BadParameter("Sketch matrix runs always use metadata from each target Arduino core")
        if not sketch.exists():
            raise typer.BadParameter(f"Sketch does not exist: {sketch}")

        test_paths = [Path(s) for s in scripts] if scripts else None
        fail_count = run_matrix(sketch, timeout_seconds=timeout, test_scripts=test_paths)
        if fail_count != 0:
            sys.exit(fail_count)
        return

    effective_firmware, mcu, effective_frequency, effective_led_pin, effective_uart = _resolve_board_target(
        board, firmware, frequency, led_builtin_pin, uart
    )
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
