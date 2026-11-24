#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] QEMU Xtensa Hardware Emulation Runner using Pigweed pw_emu."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import structlog
import typer
from pw_emu.core import Error as PwEmuError
from pw_emu.frontend import Emulator

from tools.arduino_core_metadata import read_metadata_json
from tools.boards import QEMU_BOARDS as MATRIX_BOARDS
from tools.emulation.emulation_runner import PW_EMU_CONFIG_PATH
from tools.emulation.process_utils import drain_pty_nonblocking
from tools.emulation.pw_emu_runner import (
    PwEmuTargetProfile,
    prepend_to_path,
    run_hardware_cli,
    run_hardware_matrix,
    run_pw_emu_session,
    stop_pw_emu_emulator,
)

repo_root = Path(__file__).resolve().parents[2]
logger = structlog.get_logger("qemu_runner")

QEMU_TARGET = "esp32s3"
QEMU_UART_CHANNEL = "uart0"
QEMU_SEARCH_DIRS: tuple[Path, ...] = (Path("/tmp/qemu-esp32/bin"), Path("/opt/qemu-esp32/bin"))
QEMU_PROFILE = PwEmuTargetProfile(
    engine_name="qemu",
    device_id="esp32-01",
    default_board="esp32:esp32:nano_nora",
    artifact_filename="firmware_flash.bin",
    title="QEMU Xtensa Hardware Emulation Matrix (Instruction-Accurate)",
    engine_label="QEMU Espressif (Instruction-Accurate)",
    architecture="Xtensa LX7 (ESP32-S3)",
    measurement_source="esp32_freertos_uxTaskGetStackHighWaterMark",
    watermark_log_title="QEMU ESP32-S3 runtime stack watermark recorded",
    missing_tool_label="⚠️ Skipped (QEMU binary not installed)",
)

stop_qemu_emulator = functools.partial(stop_pw_emu_emulator, label="qemu")


def supports_esp32s3(binary_path: str) -> bool:
    """Verify that the QEMU binary supports the esp32s3 machine (Espressif fork)."""
    try:
        res = subprocess.run(
            [binary_path, "-machine", "help"], capture_output=True, text=True, timeout=2.0, check=False
        )
        return "esp32s3" in res.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def find_qemu_binary() -> str | None:
    """Locate the Espressif qemu-system-xtensa binary with esp32s3 support."""
    for directory in QEMU_SEARCH_DIRS:
        candidate = directory / "qemu-system-xtensa"
        if candidate.is_file() and os.access(candidate, os.X_OK) and supports_esp32s3(str(candidate)):
            return str(candidate)
    if (found := shutil.which("qemu-system-xtensa")) is not None and supports_esp32s3(found):
        return found
    return None


def build_esp32s3_drive_arg(flash_image: Path, wdir: Path | None = None) -> str:
    """Build the QEMU ``-drive`` argument for an ESP32-S3 raw SPI flash image."""
    resolved = flash_image.resolve()
    drive_path = resolved
    if " " in str(resolved) and wdir is not None:
        wdir.mkdir(parents=True, exist_ok=True)
        link_path = wdir / "firmware_flash.bin"
        if link_path.is_symlink() or link_path.exists():
            link_path.unlink()
        link_path.symlink_to(resolved)
        drive_path = link_path
    return f"-drive file={drive_path},if=mtd,format=raw"


def start_qemu_emulator(
    wdir: Path,
    flash_image: Path,
    target: str = QEMU_TARGET,
    *,
    pause: bool = False,
) -> tuple[Emulator, str]:
    """Start an ESP32-S3 target via Pigweed ``pw_emu.frontend.Emulator`` and return ``(emu, pty_path)``."""
    qemu_bin = find_qemu_binary()
    if qemu_bin is None:
        raise PwEmuError("Espressif qemu-system-xtensa executable with esp32s3 support not found")

    drive_arg = build_esp32s3_drive_arg(flash_image, wdir=wdir)
    emu = Emulator(wdir, config_path=PW_EMU_CONFIG_PATH)
    logger.info("Starting QEMU via pw_emu", target=target, flash_image=str(flash_image), uart_channel=QEMU_UART_CHANNEL)
    # Pass file=None so QemuLauncher boots from MTD flash, paused (-S) until QMP resolves the PTY.
    with prepend_to_path(Path(qemu_bin).parent):
        emu.start(target=target, file=None, pause=True, args=drive_arg)

    try:
        pty_path = emu.get_channel_path(QEMU_UART_CHANNEL)
        if not pause:
            emu.cont()
    except (PwEmuError, OSError):
        stop_qemu_emulator(emu)
        raise

    logger.info("pw_emu QEMU UART PTY ready", channel=QEMU_UART_CHANNEL, pty=pty_path)
    return emu, pty_path


def run_qemu_emulation(
    flash_image: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    led_builtin_pin: int = 13,
    log_dir: Path | None = None,
) -> bool:
    """Run full QEMU ESP32-S3 emulation through Pigweed pw_emu and verify client tests."""
    if not flash_image.exists():
        logger.error("Flash image not found", path=str(flash_image))
        return False
    if find_qemu_binary() is None:
        logger.warning("qemu-system-xtensa executable not found")
        return False

    def _drain_boot_log(pty_path: str) -> None:
        boot_output = drain_pty_nonblocking(pty_path, timeout=1.0)[-4096:]
        try:
            boot_text: str | None = boot_output.decode("utf-8")
        except UnicodeDecodeError:
            boot_text = None
        logger.info(
            "ESP32 boot output before daemon start",
            length=len(boot_output),
            text=boot_text,
            raw=b"" if boot_text else boot_output,
        )

    return run_pw_emu_session(
        QEMU_PROFILE,
        flash_image,
        lambda wdir: start_qemu_emulator(wdir, flash_image),
        led_builtin_pin=led_builtin_pin,
        test_scripts=test_scripts,
        timeout_seconds=timeout_seconds,
        log_dir=log_dir,
        on_started=_drain_boot_log,
    )


def run_matrix(
    sketch_path: Path,
    timeout_seconds: float = 90.0,
    test_scripts: list[Path] | None = None,
) -> int:
    """Execute the QEMU Xtensa emulation matrix natively in Python (Rule 37)."""

    def _run_board(
        _fqbn: str, out_dir: Path, img: Path, summary_dir: Path, scripts: list[Path], timeout: float
    ) -> bool:
        meta = out_dir / "arduino_core_metadata.json"
        led_pin = read_metadata_json(meta).led_builtin if meta.exists() else 13
        return run_qemu_emulation(img, scripts, timeout_seconds=timeout, led_builtin_pin=led_pin, log_dir=summary_dir)

    return run_hardware_matrix(
        QEMU_PROFILE,
        sketch_path,
        boards=MATRIX_BOARDS,
        repo_root=repo_root,
        run_board=_run_board,
        timeout_seconds=timeout_seconds,
        test_scripts=test_scripts,
        tool_installed=find_qemu_binary() is not None,
    )


app = typer.Typer(
    help="Instruction-accurate Xtensa LX7 (ESP32-S3) hardware emulation using Pigweed pw_emu + QEMU.",
    add_completion=False,
)


@app.command()
def main(
    firmware: Annotated[
        Path | None, typer.Option("--firmware", "-f", help="Path to merged 16 MiB ESP32-S3 flash binary")
    ] = None,
    board: Annotated[str, typer.Option("--board", "-b", help="Target board FQBN")] = "esp32:esp32:nano_nora",
    sketch: Annotated[
        Path | None, typer.Option("--sketch", "-s", help="Arduino sketch (.ino) to compile and emulate")
    ] = None,
    scripts: Annotated[list[str] | None, typer.Argument(help="Test scripts to run")] = None,
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout per client script in seconds")] = 90.0,
) -> None:
    """Entrypoint for the QEMU Xtensa hardware emulation runner."""
    run_hardware_cli(
        QEMU_PROFILE,
        firmware=firmware,
        board=board,
        sketch=sketch,
        scripts=scripts,
        timeout=timeout,
        run_matrix_fn=run_matrix,
        run_single_fn=lambda fw, test_paths, t: run_qemu_emulation(fw, test_paths, timeout_seconds=t),
    )


if __name__ == "__main__":
    app()
