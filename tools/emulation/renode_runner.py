#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Renode ARM Hardware Emulation Runner using Pigweed pw_emu.

Executes instruction-accurate ARM Cortex-M0+ emulation using Renode via
``pw_emu.frontend.Emulator`` and ``pw_emu.renode`` for Arduino MKR WiFi 1010
(ATSAMD21G18A). Connects Renode's native UART PTY directly to the mcubridge
daemon for 100% E2E client verification.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Annotated, Any, cast

import structlog
import typer
from pw_emu.core import Error as PwEmuError
from pw_emu.frontend import Emulator

from mcubridge.config.logging import configure_logging
from tools.arduino_core_metadata import read_metadata_json
from tools.boards import RENODE_BOARDS as MATRIX_BOARDS
from tools.emulation.emulation_runner import (
    CLOUD_HOST,
    CLOUD_PORT,
    PW_EMU_CONFIG_PATH,
    EmulationState,
    audit_post_execution_status,
    default_client_test_paths,
    ensure_cloud_gateway,
    resolve_firmware_token_database,
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
from tools.emulation.stack_metrics import (
    extract_latest_mcu_stack_watermark,
    write_stack_watermark_report,
)

repo_root = Path(__file__).resolve().parents[2]

RENODE_TARGET = "mkrwifi1010"
RENODE_UART_CHANNEL = "uart0"
RENODE_SEARCH_DIRS: tuple[Path, ...] = (
    Path("/tmp/renode"),
    Path("/opt/renode"),
    Path("/usr/local/bin"),
)
SAMD21_VECTOR_TABLE_OFFSET = 0x2000
SAMD21_INIT_WRITES: tuple[tuple[int, int], ...] = (
    # SYSCTRL PCLKSR: report all SAMD21 clocks ready.
    (0x4000080C, 0x0000FFFF),
    # NVM software calibration rows.
    (0x00806020, 0xFFFFFFFF),
    (0x00806024, 0xFFFFFFFF),
    # NVMCTRL INTFLAG.READY so FlashStorage erase/write polls complete (ADR-0005).
    (0x41004014, 0x00000001),
)

configure_logging(console=True)
logger = structlog.get_logger("renode_runner")


def find_renode_binary() -> str | None:
    """Locate the Renode executable via PATH or standard installation directories."""
    found = shutil.which("renode")
    if found is not None:
        return found

    for directory in RENODE_SEARCH_DIRS:
        candidate = directory / "renode"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


@contextlib.contextmanager
def _prepend_to_path(directory: Path) -> Generator[None]:
    """Temporarily prepend ``directory`` to PATH so ``pw_emu`` resolves the selected binary."""
    old_path = os.environ.get("PATH")
    dir_str = str(directory)
    path_entries = old_path.split(os.pathsep) if old_path else []
    if path_entries and path_entries[0] == dir_str:
        yield
        return
    os.environ["PATH"] = f"{dir_str}{os.pathsep}{old_path}" if old_path else dir_str
    try:
        yield
    finally:
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path


def _effective_renode_config(wdir: Path, target: str) -> Path:
    """Return canonical pw_emu config, resolving relative platform paths if CWD != repo_root."""
    cfg_path = PW_EMU_CONFIG_PATH
    repl_rel = Path("tools") / "emulation" / "renode" / "mkrwifi1010.repl"
    if (Path.cwd() / repl_rel).is_file():
        return cfg_path

    raw_obj: object = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw_obj, dict):
        return cfg_path

    raw = cast(dict[str, Any], raw_obj)
    pw_emu_cfg = raw.get("pw", {}).get("pw_emu", {})
    target_files = pw_emu_cfg.get("target_files")
    if isinstance(target_files, list):
        resolved_files: list[str] = []
        for entry in cast(list[object], target_files):
            entry_str = str(entry)
            resolved_files.append(
                entry_str if Path(entry_str).is_absolute() else str((cfg_path.parent / entry_str).resolve())
            )
        pw_emu_cfg["target_files"] = resolved_files
    raw_renode_target = pw_emu_cfg.get("targets", {}).get(target, {}).get("renode")
    if isinstance(raw_renode_target, dict):
        renode_target_cfg = cast(dict[str, Any], raw_renode_target)
        machine: object = renode_target_cfg.get("machine")
        if isinstance(machine, str) and not Path(machine).is_absolute():
            renode_target_cfg["machine"] = str((repo_root / machine).resolve())

    wdir.mkdir(parents=True, exist_ok=True)
    resolved_cfg = wdir / "pw_emu_renode_config.json"
    resolved_cfg.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return resolved_cfg


def configure_samd21_renode(emu: Emulator, firmware_path: Path, *, resume: bool = True) -> None:
    """Pause/reset Renode, load the SAMD21 ELF, program VTOR/SP/PC and clocks, and optionally resume."""
    # RenodeLauncher._post_start() connects and immediately closes the GDB probe socket,
    # which triggers Renode's GDB server to auto-start execution at PC=0x0 (empty bootloader
    # space -> Cortex-M LOCKUP). Pause and reset the machine before loading the ELF at 0x2000.
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

    config_path = _effective_renode_config(wdir, target)
    emu = Emulator(wdir, config_path=config_path)
    logger.info(
        "Starting Renode via pw_emu",
        target=target,
        firmware=str(firmware_path),
        uart_channel=RENODE_UART_CHANNEL,
        wdir=str(wdir),
    )
    with _prepend_to_path(Path(renode_bin).parent):
        emu.start(target=target, file=None, pause=True)

    try:
        pty_path = emu.get_channel_path(RENODE_UART_CHANNEL)
        configure_samd21_renode(emu, firmware_path, resume=not pause)
    except (PwEmuError, OSError):
        stop_renode_emulator(emu)
        raise

    logger.info("pw_emu Renode UART PTY ready", channel=RENODE_UART_CHANNEL, pty=pty_path)
    return emu, pty_path


def stop_renode_emulator(emu: Emulator | None) -> None:
    """Stop a running ``pw_emu`` Renode Emulator cleanly without masking prior errors."""
    if emu is None:
        return
    try:
        emu.stop()
    except (PwEmuError, OSError) as exc:
        logger.debug("Ignoring expected error while stopping pw_emu renode", error=str(exc))


def _teardown_renode(
    daemon_proc: subprocess.Popen[Any] | None,
    emu: Emulator | None,
    gateway_proc: subprocess.Popen[Any] | None,
    fake_uci_dir: Path | str | None = None,
    storage_path: Path | str | None = None,
) -> None:
    procs = [p for p in (daemon_proc, gateway_proc) if p is not None]
    terminate_process_tree(procs, timeout=5.0)
    stop_renode_emulator(emu)
    cleanup_paths(fake_uci_dir, storage_path)


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

    state = EmulationState()
    wdir = Path(tempfile.mkdtemp(prefix="pw_emu_renode_"))
    renode_log = wdir / "renode.log"
    emu: Emulator | None = None
    daemon_proc: subprocess.Popen[str] | None = None
    gateway_proc: subprocess.Popen[str] | None = None
    fake_uci_dir: Path | None = None
    storage_path: Path | None = None

    try:
        gateway_proc = ensure_cloud_gateway(state)
        if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=15.0):
            logger.error("Gateway did not start in time")
            return False

        try:
            emu, pty_path = start_renode_emulator(wdir, firmware_path)
        except (PwEmuError, OSError) as exc:
            logger.error("Failed to start Renode via pw_emu", error=str(exc))
            return False

        fake_uci_dir, storage_path, daemon_env = setup_fake_uci(
            pty_path,
            device_id="mkr-01",
            token_database=resolve_firmware_token_database(firmware_path),
            label="renode runner",
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
        if not state.sync_event.wait(timeout=max(timeout_seconds, 150.0)):
            logger.error("MCU/daemon link failed to synchronize within timeout")
            return False

        logger.info("MCU/daemon link synchronized successfully! Waiting for post-handshake capabilities...")
        state.capabilities_event.wait(timeout=30.0)
        time.sleep(1.0)

        return (
            run_client_scripts(test_scripts, device_id="mkr-01", led_builtin_pin=led_builtin_pin)
            and audit_post_execution_status()
        )

    finally:
        _teardown_renode(daemon_proc, emu, gateway_proc, fake_uci_dir, storage_path)
        with state.lock:
            log_lines = [line for _src, line in state.output_lines]
        watermark = extract_latest_mcu_stack_watermark(log_lines)
        if watermark is not None:
            metrics_dir_str = os.environ.get("RENODE_METRICS_DIR")
            metrics_dir = Path(metrics_dir_str) if metrics_dir_str else (log_dir or firmware_path.parent)
            target_slug = firmware_path.parent.name or "arduino-samd-mkrwifi1010"
            report_path = write_stack_watermark_report(
                metrics_dir,
                target_slug,
                stack_used_bytes=int(watermark["stack_used_bytes"]),
                stack_budget_bytes=int(watermark["stack_budget_bytes"]),
                stack_margin_bytes=int(watermark["stack_margin_bytes"]),
                extra={
                    "arch": watermark["arch"],
                    "measurement_source": "samd21_sram_stack_paint_0xa5",
                },
            )
            logger.info(
                "Renode SAMD21 runtime stack watermark recorded",
                target=target_slug,
                stack_used_bytes=watermark["stack_used_bytes"],
                stack_budget_bytes=watermark["stack_budget_bytes"],
                stack_margin_bytes=watermark["stack_margin_bytes"],
                report=str(report_path),
            )
        if log_dir is not None:
            log_file = log_dir / "renode_execution.log"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                with log_file.open("w", encoding="utf-8") as f:
                    for src, line in state.output_lines:
                        f.write(f"[{src}] {line}\n")
                    if renode_log.exists():
                        f.write("\n=== RENODE INTERNAL LOG ===\n")
                        f.write(renode_log.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Could not write renode execution log", error=str(exc))
        cleanup_paths(wdir)


def run_matrix(
    sketch_path: Path,
    timeout_seconds: float = 90.0,
    test_scripts: list[Path] | None = None,
) -> int:
    """Execute multi-board Renode ARM emulation matrix natively in Python (Rule 37)."""
    build_base_dir = repo_root / "build" / "renode"
    summary_dir_str = os.environ.get("RENODE_METRICS_DIR")
    summary_dir = Path(summary_dir_str) if summary_dir_str else build_base_dir
    summary_dir.mkdir(parents=True, exist_ok=True)
    build_base_dir.mkdir(parents=True, exist_ok=True)

    test_paths = default_client_test_paths() if test_scripts is None else test_scripts
    compile_script = repo_root / "tools" / "ci" / "compile_renode_firmware.sh"
    renode_installed = find_renode_binary() is not None

    compilation_status: list[str] = []
    emulation_status: list[str] = []
    fail_count = 0

    for board_fqbn, board_name in MATRIX_BOARDS:
        out_dir = build_base_dir / board_fqbn.replace(":", "-")
        out_dir.mkdir(parents=True, exist_ok=True)
        firmware_elf = out_dir / "firmware.elf"
        firmware_elf.unlink(missing_ok=True)

        logger.info("=" * 80)
        logger.info("Testing Board in Renode ARM matrix", board=board_fqbn, name=board_name)
        logger.info("=" * 80)

        compile_res = subprocess.run(
            ["bash", str(compile_script), str(sketch_path), board_fqbn, str(out_dir)],
            check=False,
        )

        if compile_res.returncode == 0 and firmware_elf.exists():
            compilation_status.append("✅ Compiled")
            if not renode_installed:
                emulation_status.append("⚠️ Skipped (Renode binary not installed)")
                continue

            metadata_path = out_dir / "arduino_core_metadata.json"
            led_pin = read_metadata_json(metadata_path).led_builtin if metadata_path.exists() else 6

            success = run_renode_emulation(
                firmware_path=firmware_elf,
                test_scripts=test_paths,
                timeout_seconds=timeout_seconds,
                led_builtin_pin=led_pin,
                log_dir=summary_dir,
            )
            if success:
                logger.info("Renode Emulation PASSED for board", board=board_fqbn)
                emulation_status.append("✅ Passed (100% E2E)")
            else:
                logger.error("Renode Emulation FAILED for board", board=board_fqbn)
                emulation_status.append("❌ Failed")
                fail_count += 1
        elif compile_res.returncode == 0:
            compilation_status.append("⚠️ Skipped")
            emulation_status.append("⏭️ Skipped (No binary)")
        else:
            compilation_status.append("❌ Failed")
            emulation_status.append("⏭️ Not run (compilation failed)")
            fail_count += 1

    write_matrix_summary(
        summary_dir,
        prefix="renode",
        title="Renode ARM Hardware Emulation Matrix (Instruction-Accurate)",
        engine_label="Renode (Instruction-Accurate)",
        architecture="ARM Cortex-M0+",
        boards=MATRIX_BOARDS,
        compilation_status=compilation_status,
        emulation_status=emulation_status,
    )
    return fail_count


app = typer.Typer(
    help="Instruction-accurate ARM hardware emulation using Pigweed pw_emu + Renode.",
    add_completion=False,
)


@app.command()
def main(
    firmware: Annotated[
        Path | None,
        typer.Option("--firmware", "-f", help="Path to ARM ELF firmware binary"),
    ] = None,
    board: Annotated[
        str,
        typer.Option("--board", "-b", help="Target board FQBN"),
    ] = "arduino:samd:mkrwifi1010",
    sketch: Annotated[
        Path | None,
        typer.Option("--sketch", "-s", help="Path to Arduino sketch (.ino) to compile and emulate"),
    ] = None,
    scripts: Annotated[
        list[str] | None,
        typer.Argument(help="Test scripts to run"),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option("--timeout", "-t", help="Timeout per client script in seconds"),
    ] = 90.0,
) -> None:
    """Entrypoint for the Renode ARM hardware emulation runner."""
    if sketch is not None:
        if not sketch.exists():
            raise typer.BadParameter(f"Sketch does not exist: {sketch}")
        test_paths = [Path(s) for s in scripts] if scripts else None
        fail_count = run_matrix(sketch, timeout_seconds=timeout, test_scripts=test_paths)
        if fail_count != 0:
            sys.exit(fail_count)
        return

    effective_firmware = firmware or Path(f"build/renode/{board.replace(':', '-')}/firmware.elf")
    if not effective_firmware.exists():
        logger.warning("Firmware binary not found for Renode emulation", path=str(effective_firmware))
        sys.exit(1)

    test_paths = [Path(s) for s in scripts] if scripts else default_client_test_paths()
    success = run_renode_emulation(
        firmware_path=effective_firmware,
        test_scripts=test_paths,
        timeout_seconds=timeout,
    )
    if not success:
        logger.error("Renode ARM emulation suite failed!")
        sys.exit(1)

    logger.info("Renode ARM emulation completed successfully!")


if __name__ == "__main__":
    app()
