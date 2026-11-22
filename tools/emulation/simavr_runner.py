#!/usr/bin/env python3
"""Hardware-accurate AVR CPU Emulation Runner using Pigweed pw_emu + simavr.

Executes AVR ELF binaries in simavr through ``pw_emu.frontend.Emulator`` and
``pw_emu_avr``, binding the virtual UART PTY to the mcubridge Python daemon and
running full E2E client verification suites.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, cast

import structlog
import typer
from pw_emu.core import Error as PwEmuError
from pw_emu.frontend import Emulator
from pw_emu_avr.diagnostics import symbolize_diagnostics

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
from tools.emulation.stack_metrics import stack_watermark_metrics, write_stack_watermark_report

repo_root = Path(__file__).resolve().parents[2]

configure_logging(console=True)
logger = structlog.get_logger("simavr_runner")

SIMAVR_STARTUP_SETTLE_SECONDS = 5.0


def pw_emu_config_path() -> Path:
    """Return the canonical Pigweed pw_emu configuration for this repository."""
    return repo_root / "tools" / "emulation" / "pw_emu_config.json"


def build_simavr_harness() -> Path | None:
    """Build the host-side pw_emu_avr simavr_pty_runner binary if stale or missing."""
    harness_src = repo_root / "third_party" / "avr_pigweed_port" / "tools" / "pw_emu_avr" / "simavr_pty_runner.c"
    harness_bin = repo_root / "build" / "simavr" / "simavr_pty_runner"
    if not harness_src.exists():
        return None

    if harness_bin.exists() and harness_bin.stat().st_mtime >= harness_src.stat().st_mtime:
        return harness_bin

    harness_bin.parent.mkdir(parents=True, exist_ok=True)

    compile_cmd = [
        "gcc",
        "-std=c11",
        "-O2",
        "-Wall",
        "-Wextra",
        "-I/usr/include/simavr",
        "-I/usr/local/include/simavr",
        str(harness_src),
        "-lsimavr",
        "-lutil",
        "-o",
        str(harness_bin),
    ]
    try:
        res = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and harness_bin.exists():
            logger.info("Compiled pw_emu_avr simavr_pty_runner binary", binary=str(harness_bin))
            return harness_bin
        logger.warning("Failed to compile simavr_pty_runner via gcc", stderr=res.stderr)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("gcc not available to build simavr_pty_runner", error=str(exc))
    return None


def start_simavr_emulator(
    wdir: Path,
    firmware_path: Path,
    mcu: str,
    frequency: int,
    uart_id: str | None = None,
    *,
    pause: bool = False,
) -> tuple[Emulator, str]:
    """Start an AVR target via Pigweed ``pw_emu.frontend.Emulator`` and return ``(emu, pty_path)``."""
    config_path = pw_emu_config_path()
    emu = Emulator(wdir, config_path=config_path)
    extra_args = f"--frequency {frequency}" if frequency > 0 else None
    uart_channel = f"uart{uart_id or '0'}"
    logger.info(
        "Starting simavr via pw_emu",
        target=mcu,
        frequency=frequency,
        firmware=str(firmware_path),
        uart_channel=uart_channel,
        wdir=str(wdir),
    )
    emu.start(
        target=mcu,
        file=firmware_path,
        pause=pause,
        args=extra_args,
    )
    pty_path = emu.get_channel_path(uart_channel)
    logger.info("pw_emu simavr UART PTY ready", channel=uart_channel, pty=pty_path)
    return emu, pty_path


def stop_simavr_emulator(emu: Emulator | None) -> None:
    """Stop a running ``pw_emu`` Emulator cleanly without masking prior errors."""
    if emu is None:
        return
    try:
        emu.stop()
    except (PwEmuError, OSError) as exc:
        logger.debug("Ignoring expected error while stopping pw_emu simavr", error=str(exc))


def report_simavr_diagnostics(firmware_path: Path, wdir: Path) -> dict[str, Any] | None:
    """Symbolize ``diagnostics.json`` produced by ``simavr_pty_runner`` and persist watermark metrics."""
    diag_path = wdir / "diagnostics.json"
    if not diag_path.is_file():
        logger.warning("simavr exited without writing diagnostics.json", wdir=str(wdir))
        return None

    try:
        raw_obj = json.loads(diag_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed reading simavr diagnostics.json", path=str(diag_path), error=str(exc))
        return None

    if not isinstance(raw_obj, dict):
        logger.warning("Unexpected non-object diagnostics.json payload", path=str(diag_path))
        return None

    raw_diag = cast(dict[str, Any], raw_obj)
    sym = symbolize_diagnostics(firmware_path, raw_diag)
    crashed = bool(sym.get("crashed", False))
    min_sp = sym.get("min_sp")
    ramend = sym.get("ramend")
    heap_start = sym.get("heap_start")
    crash_pc = sym.get("crash_pc")
    crash_sp = sym.get("crash_sp")

    log = logger.error if crashed else logger.info
    log(
        "simavr firmware exit diagnostics",
        crashed=crashed,
        pc=hex(crash_pc) if isinstance(crash_pc, int) else None,
        function=sym.get("crash_function"),
        sp=hex(crash_sp) if isinstance(crash_sp, int) else None,
        stack_low_water=hex(min_sp) if isinstance(min_sp, int) else None,
        heap_start=hex(heap_start) if isinstance(heap_start, int) else None,
        stack_overflowed_into_bss=sym.get("stack_overflowed_into_bss"),
    )

    if crashed:
        simavr_log = wdir / "simavr.log"
        if simavr_log.is_file():
            try:
                tail = simavr_log.read_text(encoding="utf-8").splitlines()[-20:]
                if tail:
                    logger.error("simavr runner log tail", lines=tail)
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("Could not read simavr.log", error=str(exc))

    if not isinstance(min_sp, int) or not isinstance(ramend, int):
        return sym

    metrics = stack_watermark_metrics(
        min_sp,
        ramend,
        heap_start if isinstance(heap_start, int) else None,
    )
    peak_pc = sym.get("peak_pc")
    peak_function = sym.get("peak_function")
    call_chain = sym.get("call_chain_candidates", [])
    logger.info(
        "simavr stack peak context",
        stack_used_bytes=metrics["stack_used_bytes"],
        stack_budget_bytes=metrics["stack_budget_bytes"],
        stack_margin_bytes=metrics["stack_margin_bytes"],
        pc=hex(peak_pc) if isinstance(peak_pc, int) else None,
        function=peak_function,
        call_chain_candidates=call_chain,
    )

    stack_used = metrics["stack_used_bytes"]
    if stack_used is not None:
        metrics_dir = Path(os.environ.get("SIMAVR_METRICS_DIR", firmware_path.parent))
        write_stack_watermark_report(
            metrics_dir,
            firmware_path.parent.name,
            stack_used_bytes=stack_used,
            stack_budget_bytes=metrics["stack_budget_bytes"],
            stack_margin_bytes=metrics["stack_margin_bytes"],
            extra={
                "min_sp": min_sp,
                "ramend": ramend,
                "heap_start": heap_start,
                "peak_pc": peak_pc,
                "peak_function": peak_function,
                "call_chain_candidates": call_chain,
            },
        )
    return sym


def _teardown_simavr(
    daemon_proc: subprocess.Popen[Any] | None,
    emu: Emulator | None,
    gateway_proc: subprocess.Popen[Any] | None,
    fake_uci_dir: Path | str | None = None,
    storage_path: Path | str | None = None,
) -> None:
    procs = [p for p in (daemon_proc, gateway_proc) if p is not None]
    terminate_process_tree(procs, timeout=5.0)
    stop_simavr_emulator(emu)
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
    """Run full E2E tests against an AVR ELF running in simavr via Pigweed pw_emu."""
    if not firmware_path.exists():
        logger.error("Firmware ELF not found", path=str(firmware_path))
        return False

    harness_bin = build_simavr_harness()
    if harness_bin is None:
        logger.error("simavr_pty_runner could not be built; install libsimavr-dev and gcc")
        return False

    state = EmulationState()
    wdir = Path(tempfile.mkdtemp(prefix="pw_emu_simavr_"))
    emu: Emulator | None = None
    daemon_proc: subprocess.Popen[str] | None = None
    gateway_proc: subprocess.Popen[str] | None = None
    fake_uci_dir: Path | None = None
    storage_path: Path | None = None

    try:
        gateway_proc = ensure_cloud_gateway(state)
        if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=1.0):
            return False

        try:
            emu, slave_name = start_simavr_emulator(wdir, firmware_path, mcu, frequency, uart_id)
        except (PwEmuError, OSError) as exc:
            logger.error("Failed to start simavr via pw_emu", error=str(exc))
            return False

        # Allow AVR cryptographic POST to finish before the daemon opens the UART.
        time.sleep(SIMAVR_STARTUP_SETTLE_SECONDS)
        fake_uci_dir, storage_path, daemon_env = setup_fake_uci(
            slave_name,
            device_id="yun-01",
            label="simavr runner",
        )

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
            return False

        logger.info("Waiting for MCU/daemon link cryptographic synchronization...")
        if not state.sync_event.wait(timeout=max(timeout_seconds, 60.0)):
            return False

        logger.info("MCU/daemon link synchronized successfully! Waiting for post-handshake capabilities...")
        state.capabilities_event.wait(timeout=30.0)
        time.sleep(1.0)

        return run_client_scripts(test_scripts, led_builtin_pin=led_builtin_pin) and audit_post_execution_status()
    finally:
        _teardown_simavr(daemon_proc, emu, gateway_proc, fake_uci_dir, storage_path)
        if emu is not None:
            report_simavr_diagnostics(firmware_path, wdir)
        cleanup_paths(wdir)


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
    help="Cycle-accurate AVR hardware emulation using Pigweed pw_emu + simavr.",
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
        uses_uart1 = mcu == "atmega2560" and ("bluetooth" in fw_str or "wifi" in fw_str)
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
        typer.Option("--uart", "-u", help="UART peripheral ID (e.g. 0, 1)"),
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
