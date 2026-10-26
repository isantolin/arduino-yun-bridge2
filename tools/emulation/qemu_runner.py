#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] QEMU Xtensa Hardware Emulation Runner.

Executes instruction-accurate Xtensa LX7 emulation of the Arduino Nano ESP32
(ESP32-S3) using the Espressif QEMU fork (``qemu-system-xtensa -machine esp32s3``).
The virtual UART0 is exposed as a TCP server, bridged to a PTY via socat and
consumed by the mcubridge daemon for 100% E2E client verification.

Runs strictly headless: ``-nographic -monitor none``.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import structlog
import typer
from tools.arduino_core_metadata import read_metadata_json
from tools.boards import QEMU_BOARDS as MATRIX_BOARDS
from tools.emulation.process_utils import (
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
    wait_for_path_ready,
    wait_for_tcp_ready,
)
from tools.emulation.renode_runner import (
    CLOUD_HOST,
    CLOUD_PORT,
    RenodeState,
    default_client_test_paths,
    ensure_cloud_gateway,
    run_client_scripts,
    setup_fake_uci,
)

repo_root = Path(__file__).resolve().parents[2]

# --- Constants ---
DEFAULT_QEMU_PORT = 12346
QEMU_MACHINE = "esp32s3"
QEMU_LISTEN_MARKER = "QEMU waiting for connection"
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
    # 1. Search known Espressif installation directories first
    for directory in QEMU_SEARCH_DIRS:
        candidate = directory / "qemu-system-xtensa"
        if candidate.is_file() and os.access(candidate, os.X_OK) and supports_esp32s3(str(candidate)):
            return str(candidate)

    # 2. Search PATH, ensuring the binary is the Espressif fork supporting esp32s3
    found = shutil.which("qemu-system-xtensa")
    if found is not None and supports_esp32s3(found):
        return found

    return None


@dataclass
class QemuState(RenodeState):
    """Process output aggregator with an extra event for the QEMU UART listener."""

    listening_event: threading.Event = field(default_factory=threading.Event)

    def on_line(self, line: str, source: str) -> None:
        if source == "qemu" and QEMU_LISTEN_MARKER in line:
            self.listening_event.set()
        super().on_line(line, source)


def build_qemu_command(qemu_bin: str, flash_image: Path, port: int) -> list[str]:
    """Build the strictly headless QEMU command line."""
    return [
        qemu_bin,
        "-nographic",
        "-monitor",
        "none",
        "-machine",
        QEMU_MACHINE,
        "-drive",
        f"file={flash_image.resolve()},if=mtd,format=raw",
        "-serial",
        f"tcp:127.0.0.1:{port},server,wait",
    ]


def audit_status() -> bool:
    status_file = Path("/tmp/mcubridge_status.json")
    if not status_file.exists():
        return True
    try:
        from tools.audit.audit_bridge_status import audit_status_dict

        status_errors = audit_status_dict(json.loads(status_file.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error("Failed auditing bridge status", error=str(exc))
        return False
    if status_errors:
        logger.error("Post-execution status health check failed", errors=status_errors)
        return False
    logger.info("Post-execution status health check passed (100% clean)")
    return True


def run_qemu_emulation(
    flash_image: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = DEFAULT_QEMU_PORT,
    led_builtin_pin: int = 13,
    log_dir: Path | None = None,
) -> bool:
    """Run full QEMU ESP32-S3 emulation with socat PTY bridge and client tests."""
    qemu_bin = find_qemu_binary()
    if qemu_bin is None:
        logger.warning("qemu-system-xtensa executable not found")
        return False

    socat_bin = shutil.which("socat")
    if socat_bin is None:
        logger.error("socat executable not found in PATH")
        return False

    state = QemuState()
    temp_dir = Path(tempfile.mkdtemp(prefix="qemu_run_"))
    pty_link = temp_dir / "ttyQEMU"

    qemu_proc: subprocess.Popen[str] | None = None
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

        fake_uci_dir, storage_path, daemon_env = setup_fake_uci(str(pty_link), device_id="esp32-01")

        qemu_cmd = build_qemu_command(qemu_bin, flash_image, port)
        logger.info("Spawning QEMU (headless)", cmd=qemu_cmd)
        qemu_proc = subprocess.Popen(
            qemu_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "qemu", qemu_proc.stdout, state.on_line, "qemu")

        # [SIL-2] Do not probe the port with a raw TCP connect: with ``server,wait`` the
        # first accepted connection releases the guest CPU. Wait for QEMU's own marker.
        if not state.listening_event.wait(timeout=15.0):
            logger.error("QEMU UART TCP server failed to become ready", port=port)
            return False

        logger.info("Bridging QEMU UART socket to PTY...", pty=str(pty_link))
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

        # Drain ROM bootloader ASCII output before spawning the daemon
        time.sleep(1.0)
        try:
            fd = os.open(str(pty_link), os.O_RDONLY | os.O_NONBLOCK)
            try:
                while True:
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if not r:
                        break
                    os.read(fd, 4096)
            finally:
                os.close(fd)
        except OSError as exc:
            logger.debug("Non-fatal PTY pre-drain exception", error=str(exc))

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

        all_passed = run_client_scripts(
            test_scripts,
            device_id="esp32-01",
            led_builtin_pin=led_builtin_pin,
        )
        return all_passed and audit_status()

    finally:
        procs = [p for p in (daemon_proc, gateway_proc, socat_proc, qemu_proc) if p is not None]
        terminate_process_tree(procs, timeout=5.0)
        if log_dir is not None:
            log_file = log_dir / "qemu_execution.log"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                with log_file.open("w", encoding="utf-8") as f:
                    for src, line in state.output_lines:
                        f.write(f"[{src}] {line}\n")
            except OSError as exc:
                logger.warning("Could not write QEMU execution log", error=str(exc))
        shutil.rmtree(temp_dir, ignore_errors=True)
        if fake_uci_dir and fake_uci_dir.exists():
            shutil.rmtree(fake_uci_dir, ignore_errors=True)
        if storage_path and storage_path.exists():
            shutil.rmtree(storage_path, ignore_errors=True)


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
            led_pin = 13
            if metadata_path.exists():
                led_pin = read_metadata_json(metadata_path).led_builtin

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

    _write_summary(summary_dir, compilation_status, emulation_status)
    return fail_count


def _write_summary(summary_dir: Path, compilation_status: list[str], emulation_status: list[str]) -> None:
    architecture = "Xtensa LX7 (ESP32-S3)"
    rows: list[str] = [
        "### 🔬 QEMU Xtensa Hardware Emulation Matrix (Instruction-Accurate)\n",
        "| Board / Target | MCU Architecture | Firmware Compilation | Hardware Emulation (PTY/UART) | Result |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]
    boards_data: list[dict[str, str]] = []
    for (board_fqbn, board_name), c_stat, e_stat in zip(
        MATRIX_BOARDS, compilation_status, emulation_status, strict=True
    ):
        if "Passed" in e_stat:
            overall = "✅ PASS"
        elif "Skipped" in e_stat:
            overall = "⏭️ SKIPPED"
        else:
            overall = "❌ FAIL"
        rows.append(f"| **{board_name}**<br>`{board_fqbn}` | {architecture} | {c_stat} | {e_stat} | **{overall}** |")
        boards_data.append(
            {
                "board_fqbn": board_fqbn,
                "board_name": board_name,
                "architecture": architecture,
                "compilation": c_stat,
                "emulation": e_stat,
                "result": overall,
            }
        )

    summary_content = "\n".join(rows) + "\n"
    (summary_dir / "qemu_summary.md").write_text(summary_content, encoding="utf-8")
    (summary_dir / "qemu_summary.json").write_text(
        json.dumps({"engine": "QEMU Espressif (Instruction-Accurate)", "boards": boards_data}, indent=2),
        encoding="utf-8",
    )
    print(summary_content)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary and not os.environ.get("CONSOLIDATE_EMULATION_SUMMARY") and "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            with Path(step_summary).open("a", encoding="utf-8") as f:
                f.write(summary_content)
        except OSError as exc:
            logger.warning("Could not write to GITHUB_STEP_SUMMARY", error=str(exc))


app = typer.Typer(
    help="Instruction-accurate Xtensa ESP32-S3 hardware emulation using Espressif QEMU.",
    add_completion=False,
)


@app.command()
def main(
    firmware: Annotated[
        Path | None,
        typer.Option("--firmware", "-f", help="Path to merged 16MB ESP32-S3 flash image"),
    ] = None,
    board: Annotated[
        str,
        typer.Option("--board", "-b", help="Target board FQBN"),
    ] = "arduino:esp32:nano_nora",
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
        typer.Option("--port", "-p", help="QEMU UART TCP server port"),
    ] = DEFAULT_QEMU_PORT,
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
    if not run_qemu_emulation(
        flash_image=effective_firmware,
        test_scripts=test_paths,
        timeout_seconds=timeout,
        port=port,
    ):
        logger.error("QEMU Xtensa emulation suite failed!")
        sys.exit(1)

    logger.info("QEMU Xtensa emulation completed successfully!")


if __name__ == "__main__":
    app()
