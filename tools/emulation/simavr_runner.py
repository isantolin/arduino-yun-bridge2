#!/usr/bin/env python3
"""Hardware-accurate AVR CPU Emulation Runner using Pigweed pw_emu + simavr."""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, cast

import structlog
import typer
from pw_emu.frontend import Emulator
from pw_emu_avr.diagnostics import symbolize_diagnostics

from mcubridge.config.logging import configure_logging
from tools.arduino_core_metadata import read_metadata_json, resolve_core_metadata
from tools.boards import SIMAVR_BOARDS as MATRIX_BOARDS
from tools.emulation.emulation_runner import PW_EMU_CONFIG_PATH, EmulationState, default_client_test_paths
from tools.emulation.pw_emu_runner import (
    PwEmuTargetProfile,
    run_hardware_matrix,
    run_pw_emu_session,
    stop_pw_emu_emulator,
)
from tools.emulation.stack_metrics import stack_watermark_metrics, write_stack_watermark_report

repo_root = Path(__file__).resolve().parents[2]
configure_logging(console=True)
logger = structlog.get_logger("simavr_runner")

SIMAVR_STARTUP_SETTLE_SECONDS = 5.0
SIMAVR_PROFILE = PwEmuTargetProfile(
    engine_name="simavr",
    device_id="yun-01",
    default_board="arduino:avr:mega",
    artifact_filename="firmware.elf",
    title="simavr AVR Hardware Emulation Matrix (Cycle-Accurate)",
    engine_label="simavr (Cycle-Accurate)",
    architecture="AVR 8-bit",
    sync_timeout_floor=60.0,
    gateway_timeout=1.0,
    skipped_compile_label="⚠️ Skipped (Flash/RAM limit exceeded)",
)

stop_simavr_emulator = functools.partial(stop_pw_emu_emulator, label="simavr")


def build_simavr_runner() -> Path | None:
    """Build the host-side pw_emu_avr simavr_pty_runner binary if stale or missing."""
    runner_src = repo_root / "third_party" / "avr_pigweed_port" / "tools" / "pw_emu_avr" / "simavr_pty_runner.c"
    runner_bin = repo_root / "build" / "simavr" / "simavr_pty_runner"
    if not runner_src.exists():
        return None
    if runner_bin.exists() and runner_bin.stat().st_mtime >= runner_src.stat().st_mtime:
        return runner_bin

    runner_bin.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "gcc",
        "-std=c11",
        "-O2",
        "-Wall",
        "-Wextra",
        "-I/usr/include/simavr",
        "-I/usr/local/include/simavr",
        str(runner_src),
        "-lsimavr",
        "-lutil",
        "-o",
        str(runner_bin),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and runner_bin.exists():
            logger.info("Compiled pw_emu_avr simavr_pty_runner binary", binary=str(runner_bin))
            return runner_bin
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
    emu = Emulator(wdir, config_path=PW_EMU_CONFIG_PATH)
    uart_channel = f"uart{uart_id or '0'}"
    logger.info(
        "Starting simavr via pw_emu",
        target=mcu,
        frequency=frequency,
        firmware=str(firmware_path),
        uart_channel=uart_channel,
    )
    emu.start(target=mcu, file=firmware_path, pause=pause, args=f"--frequency {frequency}" if frequency > 0 else None)
    pty_path = emu.get_channel_path(uart_channel)
    logger.info("pw_emu simavr UART PTY ready", channel=uart_channel, pty=pty_path)
    return emu, pty_path


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

    sym = symbolize_diagnostics(firmware_path, cast(dict[str, Any], raw_obj))
    crashed = bool(sym.get("crashed", False))
    min_sp, ramend, heap_start = sym.get("min_sp"), sym.get("ramend"), sym.get("heap_start")
    crash_pc, crash_sp = sym.get("crash_pc"), sym.get("crash_sp")

    (logger.error if crashed else logger.info)(
        "simavr firmware exit diagnostics",
        crashed=crashed,
        pc=hex(crash_pc) if isinstance(crash_pc, int) else None,
        function=sym.get("crash_function"),
        sp=hex(crash_sp) if isinstance(crash_sp, int) else None,
        stack_low_water=hex(min_sp) if isinstance(min_sp, int) else None,
        heap_start=hex(heap_start) if isinstance(heap_start, int) else None,
        stack_overflowed_into_bss=sym.get("stack_overflowed_into_bss"),
    )

    if crashed and (simavr_log := wdir / "simavr.log").is_file():
        try:
            if tail := simavr_log.read_text(encoding="utf-8").splitlines()[-20:]:
                logger.error("simavr runner log tail", lines=tail)
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Could not read simavr.log", error=str(exc))

    if not isinstance(min_sp, int) or not isinstance(ramend, int):
        return sym

    metrics = stack_watermark_metrics(min_sp, ramend, heap_start if isinstance(heap_start, int) else None)
    peak_pc, peak_function, call_chain = (
        sym.get("peak_pc"),
        sym.get("peak_function"),
        sym.get("call_chain_candidates", []),
    )
    logger.info(
        "simavr stack peak context",
        stack_used_bytes=metrics["stack_used_bytes"],
        stack_budget_bytes=metrics["stack_budget_bytes"],
        stack_margin_bytes=metrics["stack_margin_bytes"],
        pc=hex(peak_pc) if isinstance(peak_pc, int) else None,
        function=peak_function,
        call_chain_candidates=call_chain,
    )
    if (stack_used := metrics["stack_used_bytes"]) is not None:
        write_stack_watermark_report(
            Path(os.environ.get("SIMAVR_METRICS_DIR", firmware_path.parent)),
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
    if build_simavr_runner() is None:
        logger.error("simavr_pty_runner could not be built; install libsimavr-dev and gcc")
        return False

    def _after_teardown(wdir: Path, _state: EmulationState, emu: Emulator | None) -> None:
        if emu is not None:
            report_simavr_diagnostics(firmware_path, wdir)

    return run_pw_emu_session(
        SIMAVR_PROFILE,
        firmware_path,
        lambda wdir: start_simavr_emulator(wdir, firmware_path, mcu, frequency, uart_id),
        led_builtin_pin=led_builtin_pin,
        test_scripts=test_scripts,
        timeout_seconds=timeout_seconds,
        on_started=lambda _pty: time.sleep(SIMAVR_STARTUP_SETTLE_SECONDS),
        on_teardown=_after_teardown,
    )


def run_matrix(
    sketch_path: Path,
    timeout_seconds: float = 90.0,
    test_scripts: list[Path] | None = None,
) -> int:
    """Execute multi-board simavr emulation matrix natively in Python (Rule 37)."""
    sketch_str = str(sketch_path)

    def _run_board(
        board_fqbn: str,
        out_dir: Path,
        firmware_elf: Path,
        _summary_dir: Path,
        test_paths: list[Path],
        board_timeout: float,
    ) -> bool:
        metadata = read_metadata_json(out_dir / "arduino_core_metadata.json")
        if metadata.fqbn != board_fqbn:
            raise RuntimeError(f"Firmware for {board_fqbn} has metadata for {metadata.fqbn}")
        uart_id = (
            "1"
            if ("BridgeBluetooth" in sketch_str or "BridgeWiFi" in sketch_str) and board_fqbn == "arduino:avr:mega"
            else None
        )
        return run_simavr_emulation(
            firmware_path=firmware_elf,
            mcu=metadata.mcu,
            frequency=metadata.frequency_hz,
            led_builtin_pin=metadata.led_builtin,
            test_scripts=test_paths,
            timeout_seconds=board_timeout,
            uart_id=uart_id,
        )

    return run_hardware_matrix(
        SIMAVR_PROFILE,
        sketch_path,
        boards=MATRIX_BOARDS,
        repo_root=repo_root,
        run_board=_run_board,
        timeout_seconds=timeout_seconds,
        test_scripts=test_scripts,
        mirror_dir=repo_root / "simavr-logs",
    )


app = typer.Typer(help="Cycle-accurate AVR hardware emulation using Pigweed pw_emu + simavr.", add_completion=False)


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
    fw_str = str(effective_firmware).lower()
    effective_uart = uart or ("1" if mcu == "atmega2560" and ("bluetooth" in fw_str or "wifi" in fw_str) else "0")
    return effective_firmware, mcu, effective_frequency, effective_led_pin, effective_uart


@app.command()
def main(
    firmware: Annotated[Path | None, typer.Option("--firmware", "-f", help="Path to AVR ELF firmware binary")] = None,
    board: Annotated[str, typer.Option("--board", "-b", help="AVR board FQBN or MCU name")] = "arduino:avr:mega",
    frequency: Annotated[int | None, typer.Option("--frequency", "-F", help="Explicit AVR CPU clock override")] = None,
    led_builtin_pin: Annotated[int | None, typer.Option("--led-builtin-pin", help="Explicit LED_BUILTIN pin")] = None,
    sketch: Annotated[
        Path | None, typer.Option("--sketch", "-s", help="Arduino sketch (.ino) to compile and emulate")
    ] = None,
    scripts: Annotated[list[str] | None, typer.Argument(help="Test scripts to run")] = None,
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout per client script in seconds")] = 90.0,
    uart: Annotated[str | None, typer.Option("--uart", "-u", help="UART peripheral ID (e.g. 0, 1)")] = None,
) -> None:
    """Entrypoint for the simavr hardware emulation runner."""
    if sketch is not None:
        if frequency is not None or led_builtin_pin is not None:
            raise typer.BadParameter("Sketch matrix runs always use metadata from each target Arduino core")
        if not sketch.exists():
            raise typer.BadParameter(f"Sketch does not exist: {sketch}")
        fail_count = run_matrix(
            sketch, timeout_seconds=timeout, test_scripts=[Path(s) for s in scripts] if scripts else None
        )
        if fail_count != 0:
            sys.exit(fail_count)
        return

    fw, mcu, freq, led_pin, eff_uart = _resolve_board_target(board, firmware, frequency, led_builtin_pin, uart)
    test_paths = [Path(s) for s in scripts] if scripts else default_client_test_paths()
    if not run_simavr_emulation(fw, mcu, freq, led_pin, test_paths, timeout_seconds=timeout, uart_id=eff_uart):
        logger.error("simavr emulation suite failed!")
        sys.exit(1)
    logger.info("All simavr emulation tests completed successfully!")


if __name__ == "__main__":
    app()
