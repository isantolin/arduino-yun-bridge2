#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Renode ARM Hardware Emulation Runner using Pigweed pw_emu."""

from __future__ import annotations

import functools
import json
import os
import shutil
from pathlib import Path
from typing import Annotated, Any, cast

import structlog
import typer
from pw_emu.core import Error as PwEmuError
from pw_emu.frontend import Emulator

from mcubridge.config.logging import configure_logging
from tools.arduino_core_metadata import read_metadata_json
from tools.boards import RENODE_BOARDS as MATRIX_BOARDS
from tools.emulation.emulation_runner import PW_EMU_CONFIG_PATH
from tools.emulation.pw_emu_runner import (
    PwEmuTargetProfile,
    prepend_to_path,
    run_hardware_cli,
    run_hardware_matrix,
    run_pw_emu_session,
    stop_pw_emu_emulator,
)

repo_root = Path(__file__).resolve().parents[2]
configure_logging(console=True)
logger = structlog.get_logger("renode_runner")

RENODE_TARGET = "mkrwifi1010"
RENODE_UART_CHANNEL = "uart0"
RENODE_SEARCH_DIRS: tuple[Path, ...] = (Path("/tmp/renode"), Path("/opt/renode"), Path("/usr/local/bin"))
SAMD21_VECTOR_TABLE_OFFSET = 0x2000
SAMD21_INIT_WRITES: tuple[tuple[int, int], ...] = (
    (0x4000080C, 0x0000FFFF),  # SYSCTRL PCLKSR: report all SAMD21 clocks ready.
    (0x00806020, 0xFFFFFFFF),  # NVM software calibration rows.
    (0x00806024, 0xFFFFFFFF),
    (0x41004014, 0x00000001),  # NVMCTRL INTFLAG.READY for FlashStorage polls (ADR-0005).
)
RENODE_PROFILE = PwEmuTargetProfile(
    engine_name="renode",
    device_id="mkr-01",
    default_board="arduino:samd:mkrwifi1010",
    artifact_filename="firmware.elf",
    title="Renode ARM Hardware Emulation Matrix (Instruction-Accurate)",
    engine_label="Renode (Instruction-Accurate)",
    architecture="ARM Cortex-M0+",
    measurement_source="samd21_sram_stack_paint_0xa5",
    watermark_log_title="Renode SAMD21 runtime stack watermark recorded",
    missing_tool_label="⚠️ Skipped (Renode binary not installed)",
)

stop_renode_emulator = functools.partial(stop_pw_emu_emulator, label="renode")


def find_renode_binary() -> str | None:
    """Locate the Renode executable via PATH or standard installation directories."""
    if (found := shutil.which("renode")) is not None:
        return found
    for directory in RENODE_SEARCH_DIRS:
        candidate = directory / "renode"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _effective_renode_config(wdir: Path, target: str) -> Path:
    """Return canonical pw_emu config, resolving relative platform paths if CWD != repo_root."""
    cfg_path = PW_EMU_CONFIG_PATH
    if (Path.cwd() / "tools" / "emulation" / "renode" / "mkrwifi1010.repl").is_file():
        return cfg_path
    raw_obj: object = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw_obj, dict):
        return cfg_path

    raw = cast(dict[str, Any], raw_obj)
    pw_emu_cfg = raw.get("pw", {}).get("pw_emu", {})
    if isinstance(target_files := pw_emu_cfg.get("target_files"), list):
        pw_emu_cfg["target_files"] = [
            str(entry) if Path(str(entry)).is_absolute() else str((cfg_path.parent / str(entry)).resolve())
            for entry in cast(list[object], target_files)
        ]
    raw_target = pw_emu_cfg.get("targets", {}).get(target, {}).get("renode")
    if isinstance(raw_target, dict):
        target_cfg = cast(dict[str, Any], raw_target)
        machine: object = target_cfg.get("machine")
        if isinstance(machine, str) and not Path(machine).is_absolute():
            target_cfg["machine"] = str((repo_root / machine).resolve())

    wdir.mkdir(parents=True, exist_ok=True)
    resolved_cfg = wdir / "pw_emu_renode_config.json"
    resolved_cfg.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return resolved_cfg


def configure_samd21_renode(emu: Emulator, firmware_path: Path, *, resume: bool = True) -> None:
    """Pause/reset Renode, load the SAMD21 ELF, program VTOR/SP/PC and clocks, and optionally resume."""
    emu.get_property("emulation", "PauseAll")
    emu.get_property("machine", "Reset")
    emu.set_property("sysbus", "LoadELF", f"@{firmware_path.resolve()}")
    emu.set_property("sysbus.cpu", "VectorTableOffset", hex(SAMD21_VECTOR_TABLE_OFFSET))

    sp_value = str(emu.get_property("sysbus", f"ReadDoubleWord {hex(SAMD21_VECTOR_TABLE_OFFSET)}")).strip()
    pc_value = str(emu.get_property("sysbus", f"ReadDoubleWord {hex(SAMD21_VECTOR_TABLE_OFFSET + 4)}")).strip()
    if not sp_value or not pc_value:
        raise PwEmuError(
            f"SAMD21 vector table at {hex(SAMD21_VECTOR_TABLE_OFFSET)} returned empty SP/PC: "
            f"sp={sp_value!r}, pc={pc_value!r}"
        )

    emu.set_property("sysbus.cpu", "SP", sp_value)
    emu.set_property("sysbus.cpu", "PC", pc_value)
    for address, value in SAMD21_INIT_WRITES:
        emu.set_property("sysbus", "WriteDoubleWord", f"{address:#010x} {value:#010x}")
    if resume:
        emu.cont()


def start_renode_emulator(
    wdir: Path,
    firmware_path: Path,
    target: str = RENODE_TARGET,
    *,
    pause: bool = False,
) -> tuple[Emulator, str]:
    """Start a Renode ARM target via Pigweed ``pw_emu.frontend.Emulator`` and return ``(emu, pty_path)``."""
    renode_bin = find_renode_binary()
    if renode_bin is None:
        raise PwEmuError("Renode executable not found in PATH or standard installation directories")

    emu = Emulator(wdir, config_path=_effective_renode_config(wdir, target))
    logger.info(
        "Starting Renode via pw_emu", target=target, firmware=str(firmware_path), uart_channel=RENODE_UART_CHANNEL
    )
    with prepend_to_path(Path(renode_bin).parent):
        emu.start(target=target, file=None, pause=True)

    try:
        pty_path = emu.get_channel_path(RENODE_UART_CHANNEL)
        configure_samd21_renode(emu, firmware_path, resume=not pause)
    except (PwEmuError, OSError):
        stop_renode_emulator(emu)
        raise

    logger.info("pw_emu Renode UART PTY ready", channel=RENODE_UART_CHANNEL, pty=pty_path)
    return emu, pty_path


def run_renode_emulation(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    led_builtin_pin: int = 6,
    log_dir: Path | None = None,
) -> bool:
    """Run full Renode ARM emulation through Pigweed pw_emu and verify client tests."""
    if not firmware_path.exists():
        logger.error("Firmware ELF not found", path=str(firmware_path))
        return False
    if find_renode_binary() is None:
        logger.warning("Renode executable not found in PATH")
        return False
    return run_pw_emu_session(
        RENODE_PROFILE,
        firmware_path,
        lambda wdir: start_renode_emulator(wdir, firmware_path),
        led_builtin_pin=led_builtin_pin,
        test_scripts=test_scripts,
        timeout_seconds=timeout_seconds,
        log_dir=log_dir,
    )


def run_matrix(
    sketch_path: Path,
    timeout_seconds: float = 90.0,
    test_scripts: list[Path] | None = None,
) -> int:
    """Execute multi-board Renode ARM emulation matrix natively in Python (Rule 37)."""

    def _run_board(
        _fqbn: str, out_dir: Path, elf: Path, summary_dir: Path, scripts: list[Path], timeout: float
    ) -> bool:
        meta = out_dir / "arduino_core_metadata.json"
        led_pin = read_metadata_json(meta).led_builtin if meta.exists() else 6
        return run_renode_emulation(elf, scripts, timeout_seconds=timeout, led_builtin_pin=led_pin, log_dir=summary_dir)

    return run_hardware_matrix(
        RENODE_PROFILE,
        sketch_path,
        boards=MATRIX_BOARDS,
        repo_root=repo_root,
        run_board=_run_board,
        timeout_seconds=timeout_seconds,
        test_scripts=test_scripts,
        tool_installed=find_renode_binary() is not None,
    )


app = typer.Typer(
    help="Instruction-accurate ARM hardware emulation using Pigweed pw_emu + Renode.", add_completion=False
)


@app.command()
def main(
    firmware: Annotated[Path | None, typer.Option("--firmware", "-f", help="Path to ARM ELF firmware binary")] = None,
    board: Annotated[str, typer.Option("--board", "-b", help="Target board FQBN")] = "arduino:samd:mkrwifi1010",
    sketch: Annotated[
        Path | None, typer.Option("--sketch", "-s", help="Arduino sketch (.ino) to compile and emulate")
    ] = None,
    scripts: Annotated[list[str] | None, typer.Argument(help="Test scripts to run")] = None,
    timeout: Annotated[float, typer.Option("--timeout", "-t", help="Timeout per client script in seconds")] = 90.0,
) -> None:
    """Entrypoint for the Renode ARM hardware emulation runner."""
    run_hardware_cli(
        RENODE_PROFILE,
        firmware=firmware,
        board=board,
        sketch=sketch,
        scripts=scripts,
        timeout=timeout,
        run_matrix_fn=run_matrix,
        run_single_fn=lambda fw, test_paths, t: run_renode_emulation(fw, test_paths, timeout_seconds=t),
    )


if __name__ == "__main__":
    app()
