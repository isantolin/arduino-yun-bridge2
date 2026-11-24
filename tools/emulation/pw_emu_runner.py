"""Shared Pigweed ``pw_emu`` hardware emulation session and matrix orchestration."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import typer
from pw_emu.core import Error as PwEmuError
from pw_emu.frontend import Emulator

from tools.emulation.emulation_runner import (
    CLOUD_HOST,
    CLOUD_PORT,
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

logger = structlog.get_logger("pw_emu_runner")


@dataclass(frozen=True, slots=True)
class PwEmuTargetProfile:
    """Canonical metadata describing one Pigweed ``pw_emu`` hardware backend."""

    engine_name: str
    device_id: str
    default_board: str
    artifact_filename: str
    title: str
    engine_label: str
    architecture: str
    sync_timeout_floor: float = 150.0
    gateway_timeout: float = 15.0
    measurement_source: str = ""
    watermark_log_title: str = ""
    missing_tool_label: str = ""
    skipped_compile_label: str = "⚠️ Skipped"


@contextlib.contextmanager
def prepend_to_path(directory: Path) -> Generator[None]:
    """Temporarily prepend ``directory`` to PATH so ``pw_emu`` resolves the selected binary."""
    old_path = os.environ.get("PATH")
    dir_str = str(directory)
    if old_path and old_path.split(os.pathsep)[0] == dir_str:
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


def stop_pw_emu_emulator(emu: Emulator | None, *, label: str) -> None:
    """Stop a running ``pw_emu`` Emulator cleanly without masking prior errors."""
    if emu is None:
        return
    try:
        emu.stop()
    except (PwEmuError, OSError) as exc:
        logger.debug("Ignoring expected error while stopping pw_emu emulator", engine=label, error=str(exc))


def _record_watermark_and_log(
    profile: PwEmuTargetProfile,
    state: EmulationState,
    firmware_path: Path,
    wdir: Path,
    log_dir: Path | None,
) -> None:
    if profile.measurement_source:
        with state.lock:
            log_lines = [line for _src, line in state.output_lines]
        watermark = extract_latest_mcu_stack_watermark(log_lines)
        if watermark is not None:
            metrics_env = os.environ.get(f"{profile.engine_name.upper()}_METRICS_DIR")
            metrics_dir = Path(metrics_env) if metrics_env else (log_dir or firmware_path.parent)
            target_slug = firmware_path.parent.name or profile.default_board.replace(":", "-")
            report_path = write_stack_watermark_report(
                metrics_dir,
                target_slug,
                stack_used_bytes=int(watermark["stack_used_bytes"]),
                stack_budget_bytes=int(watermark["stack_budget_bytes"]),
                stack_margin_bytes=int(watermark["stack_margin_bytes"]),
                extra={"arch": watermark["arch"], "measurement_source": profile.measurement_source},
            )
            logger.info(
                profile.watermark_log_title,
                target=target_slug,
                stack_used_bytes=watermark["stack_used_bytes"],
                stack_budget_bytes=watermark["stack_budget_bytes"],
                stack_margin_bytes=watermark["stack_margin_bytes"],
                report=str(report_path),
            )
    if log_dir is not None:
        internal_log = wdir / f"{profile.engine_name}.log"
        log_file = log_dir / f"{profile.engine_name}_execution.log"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with log_file.open("w", encoding="utf-8") as f:
                for src, line in state.output_lines:
                    f.write(f"[{src}] {line}\n")
                if internal_log.exists():
                    f.write(f"\n=== {profile.engine_name.upper()} INTERNAL LOG ===\n")
                    f.write(internal_log.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not write execution log", path=str(log_file), error=str(exc))


def run_pw_emu_session(
    profile: PwEmuTargetProfile,
    firmware_path: Path,
    start_emulator: Callable[[Path], tuple[Emulator, str]],
    *,
    led_builtin_pin: int,
    test_scripts: list[Path],
    timeout_seconds: float,
    log_dir: Path | None = None,
    on_started: Callable[[str], None] | None = None,
    on_teardown: Callable[[Path, EmulationState, Emulator | None], None] | None = None,
) -> bool:
    """Execute one full E2E session against a Pigweed ``pw_emu`` hardware emulator."""
    state = EmulationState()
    wdir = Path(tempfile.mkdtemp(prefix=f"pw_emu_{profile.engine_name}_"))
    emu: Emulator | None = None
    daemon_proc: subprocess.Popen[str] | None = None
    gateway_proc: subprocess.Popen[str] | None = None
    fake_uci_dir: Path | None = None
    storage_path: Path | None = None

    try:
        gateway_proc = ensure_cloud_gateway(state)
        if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=profile.gateway_timeout):
            logger.error("Gateway did not start in time", engine=profile.engine_name)
            return False

        try:
            emu, pty_path = start_emulator(wdir)
        except (PwEmuError, OSError) as exc:
            logger.error("Failed to start emulator via pw_emu", engine=profile.engine_name, error=str(exc))
            return False

        if on_started is not None:
            on_started(pty_path)

        fake_uci_dir, storage_path, daemon_env = setup_fake_uci(
            pty_path,
            device_id=profile.device_id,
            token_database=resolve_firmware_token_database(firmware_path),
            label=f"{profile.engine_name} runner",
        )

        daemon_cmd = [sys.executable, "-u", "-m", "mcubridge.daemon"]
        logger.info("Spawning mcubridge daemon", engine=profile.engine_name, cmd=daemon_cmd)
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
            logger.error("Daemon exited prematurely", engine=profile.engine_name, returncode=daemon_proc.returncode)
            return False

        logger.info("Waiting for MCU/daemon link cryptographic synchronization...", engine=profile.engine_name)
        if not state.sync_event.wait(timeout=max(timeout_seconds, profile.sync_timeout_floor)):
            logger.error("MCU/daemon link failed to synchronize within timeout", engine=profile.engine_name)
            return False

        logger.info(
            "MCU/daemon link synchronized! Waiting for post-handshake capabilities...", engine=profile.engine_name
        )
        state.capabilities_event.wait(timeout=30.0)
        time.sleep(1.0)

        return (
            run_client_scripts(test_scripts, device_id=profile.device_id, led_builtin_pin=led_builtin_pin)
            and audit_post_execution_status()
        )
    finally:
        procs: list[subprocess.Popen[Any]] = [p for p in (daemon_proc, gateway_proc) if p is not None]
        terminate_process_tree(procs, timeout=5.0)
        stop_pw_emu_emulator(emu, label=profile.engine_name)
        cleanup_paths(fake_uci_dir, storage_path)
        if on_teardown is not None:
            on_teardown(wdir, state, emu)
        elif profile.measurement_source or log_dir is not None:
            _record_watermark_and_log(profile, state, firmware_path, wdir, log_dir)
        cleanup_paths(wdir)


def run_hardware_matrix(
    profile: PwEmuTargetProfile,
    sketch_path: Path,
    *,
    boards: Sequence[tuple[str, str]],
    repo_root: Path,
    run_board: Callable[[str, Path, Path, Path, list[Path], float], bool],
    timeout_seconds: float = 90.0,
    test_scripts: list[Path] | None = None,
    tool_installed: bool = True,
    mirror_dir: Path | None = None,
) -> int:
    """Compile and emulate every board in ``boards`` and emit Markdown/JSON matrix summaries."""
    build_base_dir = repo_root / "build" / profile.engine_name
    summary_env = os.environ.get(f"{profile.engine_name.upper()}_METRICS_DIR")
    summary_dir = Path(summary_env) if summary_env else build_base_dir
    summary_dir.mkdir(parents=True, exist_ok=True)
    build_base_dir.mkdir(parents=True, exist_ok=True)

    test_paths = default_client_test_paths() if test_scripts is None else test_scripts
    compile_script = repo_root / "tools" / "ci" / "compile_firmware.sh"
    compilation_status: list[str] = []
    emulation_status: list[str] = []
    fail_count = 0

    for board_fqbn, board_name in boards:
        out_dir = build_base_dir / board_fqbn.replace(":", "-")
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / profile.artifact_filename
        artifact.unlink(missing_ok=True)

        logger.info(
            "Testing board in hardware emulation matrix", engine=profile.engine_name, board=board_fqbn, name=board_name
        )
        compile_res = subprocess.run(
            ["bash", str(compile_script), str(sketch_path), board_fqbn, str(out_dir)],
            check=False,
        )

        if compile_res.returncode == 0 and artifact.exists():
            compilation_status.append("✅ Compiled")
            if not tool_installed:
                emulation_status.append(profile.missing_tool_label)
                continue
            if run_board(board_fqbn, out_dir, artifact, summary_dir, test_paths, timeout_seconds):
                logger.info("Emulation PASSED for board", engine=profile.engine_name, board=board_fqbn)
                emulation_status.append("✅ Passed (100% E2E)")
            else:
                logger.error("Emulation FAILED for board", engine=profile.engine_name, board=board_fqbn)
                emulation_status.append("❌ Failed")
                fail_count += 1
        elif compile_res.returncode == 0:
            compilation_status.append(profile.skipped_compile_label)
            emulation_status.append("⏭️ Skipped (No binary)")
        else:
            compilation_status.append("❌ Failed")
            emulation_status.append("⏭️ Not run (compilation failed)")
            fail_count += 1

    write_matrix_summary(
        summary_dir,
        prefix=profile.engine_name,
        title=profile.title,
        engine_label=profile.engine_label,
        architecture=profile.architecture,
        boards=boards,
        compilation_status=compilation_status,
        emulation_status=emulation_status,
        mirror_dir=mirror_dir,
    )
    return fail_count


def run_hardware_cli(
    profile: PwEmuTargetProfile,
    *,
    firmware: Path | None,
    board: str,
    sketch: Path | None,
    scripts: list[str] | None,
    timeout: float,
    run_matrix_fn: Callable[[Path, float, list[Path] | None], int],
    run_single_fn: Callable[[Path, list[Path], float], bool],
) -> None:
    """Shared CLI dispatcher for single-image and matrix hardware emulation runners."""
    if sketch is not None:
        if not sketch.exists():
            raise typer.BadParameter(f"Sketch does not exist: {sketch}")
        fail_count = run_matrix_fn(sketch, timeout, [Path(s) for s in scripts] if scripts else None)
        if fail_count != 0:
            sys.exit(fail_count)
        return

    effective_fw = firmware or Path(
        f"build/{profile.engine_name}/{board.replace(':', '-')}/{profile.artifact_filename}"
    )
    if not effective_fw.exists():
        logger.warning("Firmware artifact not found", engine=profile.engine_name, path=str(effective_fw))
        sys.exit(1)

    test_paths = [Path(s) for s in scripts] if scripts else default_client_test_paths()
    if not run_single_fn(effective_fw, test_paths, timeout):
        logger.error("Hardware emulation suite failed!", engine=profile.engine_name)
        sys.exit(1)
    logger.info("Hardware emulation completed successfully!", engine=profile.engine_name)
