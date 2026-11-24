#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] QEMU Xtensa Hardware Emulation Runner using Pigweed pw_emu.

Executes instruction-accurate Xtensa LX7 emulation of the Arduino Nano ESP32
(ESP32-S3) through ``pw_emu.frontend.Emulator`` and ``pw_emu.qemu`` using the
Espressif QEMU fork (``qemu-system-xtensa -machine esp32s3``). Virtual UART0
is exposed directly as a native PTY chardev via QMP and consumed by the
mcubridge daemon for 100% E2E client verification.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from pw_emu.core import Error as PwEmuError
from pw_emu.frontend import Emulator

from tools.arduino_core_metadata import read_metadata_json
from tools.boards import QEMU_BOARDS as MATRIX_BOARDS
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
    drain_pty_nonblocking,
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

QEMU_TARGET = "esp32s3"
QEMU_UART_CHANNEL = "uart0"
QEMU_SEARCH_DIRS: tuple[Path, ...] = (Path("/tmp/qemu-esp32/bin"), Path("/opt/qemu-esp32/bin"))

logger = structlog.get_logger("qemu_runner")


def supports_esp32s3(binary_path: str) -> bool:
    """Verify that the QEMU binary supports the esp32s3 machine (Espressif fork)."""
    try:
        res = subprocess.run(
            [binary_path, "-machine", "help"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
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

    found = shutil.which("qemu-system-xtensa")
    if found is not None and supports_esp32s3(found):
        return found

    return None


@contextlib.contextmanager
def _prepend_to_path(directory: Path) -> Generator[None]:
    """Temporarily prepend ``directory`` to PATH so ``pw_emu`` resolves the Espressif fork."""
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


def build_esp32s3_drive_arg(flash_image: Path, wdir: Path | None = None) -> str:
    """Build the QEMU ``-drive`` argument for an ESP32-S3 raw SPI flash image.

    ``pw_emu.qemu.QemuLauncher`` splits ``args`` on spaces, so flash image paths
    containing spaces are symlinked inside ``wdir`` first.
    """
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
    logger.info(
        "Starting QEMU via pw_emu",
        target=target,
        flash_image=str(flash_image),
        uart_channel=QEMU_UART_CHANNEL,
        wdir=str(wdir),
    )
    # Pass file=None so QemuLauncher does not emit -kernel; ESP32-S3 boots from MTD flash.
    # Start paused (-S) so QMP resolves the PTY chardev before ROM boot bytes are emitted.
    with _prepend_to_path(Path(qemu_bin).parent):
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


def stop_qemu_emulator(emu: Emulator | None) -> None:
    """Stop a running ``pw_emu`` QEMU Emulator cleanly without masking prior errors."""
    if emu is None:
        return
    try:
        emu.stop()
    except (PwEmuError, OSError) as exc:
        logger.debug("Ignoring expected error while stopping pw_emu qemu", error=str(exc))


def _teardown_qemu(
    daemon_proc: subprocess.Popen[Any] | None,
    emu: Emulator | None,
    gateway_proc: subprocess.Popen[Any] | None,
    fake_uci_dir: Path | str | None = None,
    storage_path: Path | str | None = None,
) -> None:
    procs = [p for p in (daemon_proc, gateway_proc) if p is not None]
    terminate_process_tree(procs, timeout=5.0)
    stop_qemu_emulator(emu)
    cleanup_paths(fake_uci_dir, storage_path)


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

    state = EmulationState()
    wdir = Path(tempfile.mkdtemp(prefix="pw_emu_qemu_"))
    qemu_log = wdir / "qemu.log"
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
            emu, pty_path = start_qemu_emulator(wdir, flash_image)
        except (PwEmuError, OSError) as exc:
            logger.error("Failed to start QEMU via pw_emu", error=str(exc))
            return False

        boot_output = drain_pty_nonblocking(pty_path, timeout=1.0)[-4096:]
        try:
            boot_text: str | None = boot_output.decode("utf-8")
        except UnicodeDecodeError:
            boot_text = None
        logger.info(
            "ESP32 boot output before daemon start",
            length=len(boot_output),
            text=boot_text,
            raw=boot_output if boot_text is None else b"",
        )

        fake_uci_dir, storage_path, daemon_env = setup_fake_uci(
            pty_path,
            device_id="esp32-01",
            token_database=resolve_firmware_token_database(flash_image),
            label="qemu runner",
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

        logger.info("MCU/daemon link synchronized! Waiting for post-handshake capabilities...")
        state.capabilities_event.wait(timeout=30.0)
        time.sleep(1.0)

        return (
            run_client_scripts(test_scripts, device_id="esp32-01", led_builtin_pin=led_builtin_pin)
            and audit_post_execution_status()
        )

    finally:
        _teardown_qemu(daemon_proc, emu, gateway_proc, fake_uci_dir, storage_path)
        with state.lock:
            log_lines = [line for _src, line in state.output_lines]
        watermark = extract_latest_mcu_stack_watermark(log_lines)
        if watermark is not None:
            metrics_dir_str = os.environ.get("QEMU_METRICS_DIR")
            metrics_dir = Path(metrics_dir_str) if metrics_dir_str else (log_dir or flash_image.parent)
            target_slug = flash_image.parent.name or "esp32-esp32-nano_nora"
            report_path = write_stack_watermark_report(
                metrics_dir,
                target_slug,
                stack_used_bytes=int(watermark["stack_used_bytes"]),
                stack_budget_bytes=int(watermark["stack_budget_bytes"]),
                stack_margin_bytes=int(watermark["stack_margin_bytes"]),
                extra={
                    "arch": watermark["arch"],
                    "measurement_source": "esp32_freertos_uxTaskGetStackHighWaterMark",
                },
            )
            logger.info(
                "QEMU ESP32-S3 runtime stack watermark recorded",
                target=target_slug,
                stack_used_bytes=watermark["stack_used_bytes"],
                stack_budget_bytes=watermark["stack_budget_bytes"],
                stack_margin_bytes=watermark["stack_margin_bytes"],
                report=str(report_path),
            )
        if log_dir is not None:
            log_file = log_dir / "qemu_execution.log"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                with log_file.open("w", encoding="utf-8") as f:
                    for src, line in state.output_lines:
                        f.write(f"[{src}] {line}\n")
                    if qemu_log.exists():
                        f.write("\n=== QEMU INTERNAL LOG ===\n")
                        f.write(qemu_log.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Could not write QEMU execution log", error=str(exc))
        cleanup_paths(wdir)


def run_matrix(
    sketch_path: Path,
    timeout_seconds: float = 90.0,
    test_scripts: list[Path] | None = None,
) -> int:
    """Execute the QEMU Xtensa emulation matrix natively in Python (Rule 37)."""
    build_base_dir = repo_root / "build" / "qemu"
    summary_dir_str = os.environ.get("QEMU_METRICS_DIR")
    summary_dir = Path(summary_dir_str) if summary_dir_str else build_base_dir
    summary_dir.mkdir(parents=True, exist_ok=True)
    build_base_dir.mkdir(parents=True, exist_ok=True)

    test_paths = default_client_test_paths() if test_scripts is None else test_scripts
    compile_script = repo_root / "tools" / "ci" / "compile_esp32_firmware.sh"
    qemu_installed = find_qemu_binary() is not None

    compilation_status: list[str] = []
    emulation_status: list[str] = []
    fail_count = 0

    for board_fqbn, board_name in MATRIX_BOARDS:
        out_dir = build_base_dir / board_fqbn.replace(":", "-")
        out_dir.mkdir(parents=True, exist_ok=True)
        flash_image = out_dir / "firmware_flash.bin"
        flash_image.unlink(missing_ok=True)

        logger.info("=" * 80)
        logger.info("Testing Board in QEMU Xtensa matrix", board=board_fqbn, name=board_name)
        logger.info("=" * 80)

        compile_res = subprocess.run(
            ["bash", str(compile_script), str(sketch_path), board_fqbn, str(out_dir)],
            check=False,
        )

        if compile_res.returncode == 0 and flash_image.exists():
            compilation_status.append("✅ Compiled")
            if not qemu_installed:
                emulation_status.append("⚠️ Skipped (QEMU binary not installed)")
                continue

            metadata_path = out_dir / "arduino_core_metadata.json"
            led_pin = read_metadata_json(metadata_path).led_builtin if metadata_path.exists() else 13

            success = run_qemu_emulation(
                flash_image=flash_image,
                test_scripts=test_paths,
                timeout_seconds=timeout_seconds,
                led_builtin_pin=led_pin,
                log_dir=summary_dir,
            )
            if success:
                logger.info("QEMU Emulation PASSED for board", board=board_fqbn)
                emulation_status.append("✅ Passed (100% E2E)")
            else:
                logger.error("QEMU Emulation FAILED for board", board=board_fqbn)
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
        prefix="qemu",
        title="QEMU Xtensa Hardware Emulation Matrix (Instruction-Accurate)",
        engine_label="QEMU Espressif (Instruction-Accurate)",
        architecture="Xtensa LX7 (ESP32-S3)",
        boards=MATRIX_BOARDS,
        compilation_status=compilation_status,
        emulation_status=emulation_status,
    )
    return fail_count


app = typer.Typer(
    help="Instruction-accurate Xtensa LX7 (ESP32-S3) hardware emulation using Pigweed pw_emu + QEMU.",
    add_completion=False,
)


@app.command()
def main(
    firmware: Annotated[
        Path | None,
        typer.Option("--firmware", "-f", help="Path to merged 16 MiB ESP32-S3 flash binary"),
    ] = None,
    board: Annotated[
        str,
        typer.Option("--board", "-b", help="Target board FQBN"),
    ] = "esp32:esp32:nano_nora",
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
    """Entrypoint for the QEMU Xtensa hardware emulation runner."""
    if sketch is not None:
        if not sketch.exists():
            raise typer.BadParameter(f"Sketch does not exist: {sketch}")
        test_paths = [Path(s) for s in scripts] if scripts else None
        fail_count = run_matrix(sketch, timeout_seconds=timeout, test_scripts=test_paths)
        if fail_count != 0:
            sys.exit(fail_count)
        return

    effective_firmware = firmware or Path(f"build/qemu/{board.replace(':', '-')}/firmware_flash.bin")
    if not effective_firmware.exists():
        logger.warning("Flash image not found for QEMU emulation", path=str(effective_firmware))
        sys.exit(1)

    test_paths = [Path(s) for s in scripts] if scripts else default_client_test_paths()
    success = run_qemu_emulation(
        flash_image=effective_firmware,
        test_scripts=test_paths,
        timeout_seconds=timeout,
    )
    if not success:
        logger.error("QEMU Xtensa emulation suite failed!")
        sys.exit(1)

    logger.info("QEMU Xtensa emulation completed successfully!")


if __name__ == "__main__":
    app()
