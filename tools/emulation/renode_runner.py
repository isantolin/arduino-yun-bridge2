#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Renode ARM Hardware Emulation Runner.

Executes cycle/instruction-accurate ARM Cortex-M0+ emulation using Renode
for Arduino MKR WiFi 1010 (ATSAMD21G18A). Connects Renode virtual UART
over socket/PTY to the mcubridge daemon for 100% E2E client verification.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

import structlog
import typer
from mcubridge.config.logging import configure_logging
from tools.arduino_core_metadata import read_metadata_json
from tools.boards import RENODE_BOARDS as MATRIX_BOARDS
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
    wait_for_path_ready,
    wait_for_tcp_ready,
)

repo_root = Path(__file__).resolve().parents[2]
DEFAULT_RENODE_PORT = 12345

configure_logging(console=True)
logger = structlog.get_logger("renode_runner")

if Path("/tmp/renode").exists() and "/tmp/renode" not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = f"/tmp/renode{os.pathsep}{os.environ.get('PATH', '')}"


def run_renode_emulation(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = DEFAULT_RENODE_PORT,
    led_builtin_pin: int = 6,
    log_dir: Path | None = None,
) -> bool:
    """Run full Renode ARM emulation with socat PTY bridge and client tests."""
    renode_bin = shutil.which("renode")
    if renode_bin is None:
        logger.warning("Renode executable not found in PATH")
        return False

    socat_bin = shutil.which("socat")
    if socat_bin is None:
        logger.error("socat executable not found in PATH")
        return False

    state = EmulationState()
    renode_dir = repo_root / "tools" / "emulation" / "renode"
    resc_template = renode_dir / "mkrwifi1010.resc"
    repl_template = renode_dir / "mkrwifi1010.repl"

    temp_dir = Path(tempfile.mkdtemp(prefix="renode_run_"))
    temp_resc = temp_dir / "run.resc"
    renode_log = temp_dir / "renode.log"
    rendered_resc = (
        resc_template.read_text(encoding="utf-8")
        .replace("$ELF_PATH", f"@{firmware_path.resolve()}")
        .replace("$UART_PORT", str(port))
        .replace("$ORIGIN/mkrwifi1010.repl", f"@{repl_template.resolve()}")
    )
    temp_resc.write_text(f"logFile @{renode_log.resolve()} true\n{rendered_resc}\n", encoding="utf-8")
    pty_link = temp_dir / "ttyRENODE"

    renode_proc: subprocess.Popen[str] | None = None
    socat_proc: subprocess.Popen[str] | None = None
    daemon_proc: subprocess.Popen[str] | None = None
    gateway_proc: subprocess.Popen[str] | None = None
    fake_uci_dir: Path | None = None
    storage_path: Path | None = None

    try:
        gateway_proc = ensure_cloud_gateway(state)
        if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=15.0):
            logger.error("Gateway did not start in time")
            return False

        fake_uci_dir, storage_path, daemon_env = setup_fake_uci(
            str(pty_link),
            device_id="mkr-01",
            label="renode runner",
        )

        renode_cmd = [
            renode_bin,
            "--disable-gui",
            "--hide-monitor",
            "--hide-analyzers",
            "-P",
            "-1",
            "--plain",
            "-e",
            f"include @{temp_resc}",
        ]
        renode_proc = subprocess.Popen(
            renode_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "renode", renode_proc.stdout, state.on_line, "renode")

        if not wait_for_tcp_ready("127.0.0.1", port, timeout=15.0):
            logger.error("Renode UART socket terminal failed to become ready", port=port)
            return False

        logger.info("Bridging Renode UART socket to PTY...", pty=str(pty_link))
        socat_proc = subprocess.Popen(
            [socat_bin, "-d", "-d", f"PTY,link={pty_link},raw,echo=0", f"TCP:127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "socat", socat_proc.stdout, state.on_line, "socat")

        if not wait_for_path_ready(pty_link, timeout=5.0):
            logger.error("PTY bridge device never appeared", path=str(pty_link))
            return False

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
        procs = [p for p in (daemon_proc, gateway_proc, socat_proc, renode_proc) if p is not None]
        terminate_process_tree(procs, timeout=5.0)
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
        cleanup_paths(temp_dir, fake_uci_dir, storage_path)


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
    renode_installed = shutil.which("renode") is not None

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
    help="Cycle/Instruction-accurate ARM hardware emulation using Renode.",
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
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Renode UART socket terminal port"),
    ] = DEFAULT_RENODE_PORT,
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
        port=port,
    )
    if not success:
        logger.error("Renode ARM emulation suite failed!")
        sys.exit(1)

    logger.info("Renode ARM emulation completed successfully!")


if __name__ == "__main__":
    app()
