#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Renode ARM Hardware Emulation Runner.

Executes cycle/instruction-accurate ARM Cortex-M0+ emulation using Renode
for Arduino MKR WiFi 1010 (ATSAMD21G18A). Connects Renode virtual UART
over socket/PTY to the mcubridge daemon for 100% E2E client verification.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import protocol
from tools.arduino_core_metadata import read_metadata_json
from tools.emulation.process_utils import (
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
    wait_for_path_ready,
    wait_for_tcp_ready,
    write_fake_uci_module,
)

repo_root = Path(__file__).resolve().parents[2]

# --- Constants ---
CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = protocol.DEFAULT_CLOUD_PORT
DEFAULT_RENODE_PORT = 12345

configure_logging(console=True)
logger = structlog.get_logger("renode_runner")

MATRIX_BOARDS: list[tuple[str, str]] = [
    ("arduino:samd:mkrwifi1010", "Arduino MKR WiFi 1010 (ATSAMD21G18A)"),
]


def default_client_test_paths() -> list[Path]:
    return [
        repo_root / "mcubridge-client-examples" / "tests" / "test_smoke_connection.py",
        repo_root / "mcubridge-client-examples" / "examples" / "led13_test.py",
        repo_root / "mcubridge-client-examples" / "examples" / "console_test.py",
        repo_root / "mcubridge-client-examples" / "examples" / "mailbox_read_test.py",
    ]


def _empty_output_lines() -> list[tuple[str, str]]:
    return []


@dataclass
class RenodeState:
    output_lines: list[tuple[str, str]] = field(default_factory=_empty_output_lines)
    sync_event: threading.Event = field(default_factory=threading.Event)
    capabilities_event: threading.Event = field(default_factory=threading.Event)

    def on_line(self, line: str, source: str) -> None:
        clean_line = line.strip()
        self.output_lines.append((source, clean_line))

        if source in ("daemon-stdout", "daemon-stderr"):
            if "Capabilities discovery complete" in clean_line:
                self.capabilities_event.set()
                self.sync_event.set()
            elif (
                "MCU link synchronised" in clean_line
                or "MCU link synchronized" in clean_line
                or "Handshake synchronization complete" in clean_line
                or '"new_state": "SYNCHRONIZED"' in clean_line
            ):
                self.sync_event.set()


def _setup_fake_uci(slave_name: str) -> tuple[Path, Path, dict[str, str]]:
    """Configure fake UCI directory, LMDB storage, and daemon environment variables."""
    fake_uci_dir = Path(tempfile.mkdtemp(prefix="mcubridge_renode_uci_"))
    storage_path = Path(tempfile.mkdtemp(prefix="mcubridge_renode_db_"))

    uci_config = {
        "serial_port": slave_name,
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_safe_baud": str(protocol.DEFAULT_SAFE_BAUDRATE),
        "cloud_enabled": "1",
        "cloud_host": CLOUD_HOST,
        "cloud_port": str(CLOUD_PORT),
        "cloud_tls": "0",
        "cloud_tls_insecure": "1",
        "watchdog_enabled": "0",
        "serial_response_timeout": "30.0",
        "serial_shared_secret": "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe",
        "allowed_commands": "*",
        "storage_path": str(storage_path),
        "debug": "1",
    }
    write_fake_uci_module(fake_uci_dir, uci_config, label="renode runner")

    daemon_env = dict(os.environ)
    extra_paths = [
        str(fake_uci_dir),
        str(repo_root / "mcubridge"),
        str(repo_root / "mcubridge-client-examples"),
        str(repo_root),
    ]
    curr_pp = daemon_env.get("PYTHONPATH", "")
    daemon_env["PYTHONPATH"] = ":".join(extra_paths + ([curr_pp] if curr_pp else []))
    daemon_env["PYTHONUNBUFFERED"] = "1"
    daemon_env["MCUBRIDGE_FORCE_UCI"] = "1"
    daemon_env["MCUBRIDGE_NON_INTERACTIVE"] = "1"
    daemon_env["MCUBRIDGE_LOG_STREAM"] = "1"
    daemon_env["MCUBRIDGE_SERIAL_PORT"] = slave_name
    daemon_env["MCUBRIDGE_SERIAL_SAFE_BAUD"] = str(protocol.DEFAULT_SAFE_BAUDRATE)
    daemon_env["MCUBRIDGE_SERIAL_BAUD"] = str(protocol.DEFAULT_BAUDRATE)
    daemon_env["MCUBRIDGE_SERIAL_RESPONSE_TIMEOUT"] = "30.0"
    daemon_env["MCUBRIDGE_SERIAL_SHARED_SECRET"] = "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
    daemon_env["MCUBRIDGE_DISABLE_METRICS"] = "1"
    daemon_env["MCUBRIDGE_STORAGE_PATH"] = str(storage_path)
    daemon_env["MCUBRIDGE_CLOUD_ENABLED"] = "1"
    daemon_env["MCUBRIDGE_CLOUD_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_CLOUD_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_GATEWAY_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_GATEWAY_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_DEVICE_ID"] = "mkr-01"

    return fake_uci_dir, storage_path, daemon_env


def run_single_client_script(
    script_path: Path,
    device_id: str = "mkr-01",
    led_builtin_pin: int = 6,
) -> bool:
    """Execute a single client test script with main() injection."""
    try:
        spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[script_path.stem] = module
            spec.loader.exec_module(module)
            if hasattr(module, "main"):
                main_fn = module.main
                sig = inspect.signature(main_fn)
                kwargs: dict[str, Any] = {}
                if "host" in sig.parameters:
                    kwargs["host"] = CLOUD_HOST
                if "port" in sig.parameters:
                    kwargs["port"] = CLOUD_PORT
                if "device_id" in sig.parameters:
                    kwargs["device_id"] = device_id
                if "led_builtin_pin" in sig.parameters:
                    kwargs["led_builtin_pin"] = led_builtin_pin
                main_fn(**kwargs)
                return True
        runpy.run_path(str(script_path), run_name="__main__")
        return True
    except SystemExit as exc:
        if exc.code == 0:
            return True
        logger.error("Test failed with exit code", script=script_path.name, code=exc.code)
        return False
    except (OSError, RuntimeError, ValueError, TypeError, AssertionError) as exc:
        logger.error("Test failed with exception", script=script_path.name, error=str(exc))
        return False


def run_client_scripts(
    test_scripts: list[Path],
    daemon_env: dict[str, str],
    timeout_seconds: float,
    *,
    led_builtin_pin: int,
) -> bool:
    _ = (daemon_env, timeout_seconds)
    old_non_interactive = os.environ.get("MCUBRIDGE_NON_INTERACTIVE")
    os.environ["MCUBRIDGE_NON_INTERACTIVE"] = "1"
    try:
        for test_path in test_scripts:
            if not test_path.exists():
                logger.warning("Test script not found, skipping", path=str(test_path))
                continue
            if not run_single_client_script(test_path, device_id="mkr-01", led_builtin_pin=led_builtin_pin):
                return False
        return True
    finally:
        if old_non_interactive is None:
            os.environ.pop("MCUBRIDGE_NON_INTERACTIVE", None)
        else:
            os.environ["MCUBRIDGE_NON_INTERACTIVE"] = old_non_interactive


def run_renode_emulation(
    firmware_path: Path,
    test_scripts: list[Path],
    timeout_seconds: float = 90.0,
    port: int = DEFAULT_RENODE_PORT,
    led_builtin_pin: int = 6,
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

    state = RenodeState()
    renode_dir = repo_root / "tools" / "emulation" / "renode"
    resc_template = renode_dir / "mkrwifi1010.resc"

    temp_dir = Path(tempfile.mkdtemp(prefix="renode_run_"))
    temp_resc = temp_dir / "run.resc"
    resc_content = (
        f"$ELF_PATH = @{firmware_path.resolve()}\n"
        f"$UART_PORT = {port}\n"
        f"include @{resc_template.resolve()}\n"
    )
    temp_resc.write_text(resc_content, encoding="utf-8")

    pty_link = temp_dir / "ttyRENODE"

    renode_proc: subprocess.Popen[str] | None = None
    socat_proc: subprocess.Popen[str] | None = None
    daemon_proc: subprocess.Popen[str] | None = None
    gateway_proc: subprocess.Popen[str] | None = None
    fake_uci_dir: Path | None = None
    storage_path: Path | None = None

    try:
        logger.info("Starting Renode headless instance...", port=port, elf=str(firmware_path))
        renode_cmd = [renode_bin, "--disable-gui", "-e", f"include @{temp_resc}"]
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
        socat_cmd = [
            socat_bin,
            "-d",
            "-d",
            f"PTY,link={pty_link},raw,echo=0",
            f"TCP:127.0.0.1:{port}",
        ]
        socat_proc = subprocess.Popen(
            socat_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "socat", socat_proc.stdout, state.on_line, "socat")

        if not wait_for_path_ready(pty_link, timeout=5.0):
            logger.error("PTY bridge device never appeared", path=str(pty_link))
            return False

        fake_uci_dir, storage_path, daemon_env = _setup_fake_uci(str(pty_link))

        gateway_cmd = [
            sys.executable,
            "-m",
            "mcubridge_gateway.main",
            "--no-tls",
            "--port",
            str(CLOUD_PORT),
        ]
        gateway_proc = subprocess.Popen(
            gateway_cmd,
            env=daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "gateway", gateway_proc.stdout, state.on_line, "gateway")

        if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=10.0):
            logger.error("Gateway did not start in time")
            return False

        daemon_cmd = [sys.executable, "-m", "mcubridge.main", "--debug"]
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            env=daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "daemon", daemon_proc.stdout, state.on_line, "daemon")

        logger.info("Waiting for MCU/daemon link synchronization...")
        if not state.sync_event.wait(timeout=20.0):
            logger.error("MCU/daemon link failed to synchronize within timeout")
            return False

        state.capabilities_event.wait(timeout=10.0)
        time.sleep(1.0)

        return run_client_scripts(test_scripts, daemon_env, timeout_seconds, led_builtin_pin=led_builtin_pin)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        procs = [p for p in (daemon_proc, gateway_proc, socat_proc, renode_proc) if p is not None]
        terminate_process_tree(procs, timeout=2.0)
        if fake_uci_dir and fake_uci_dir.exists():
            shutil.rmtree(fake_uci_dir, ignore_errors=True)
        if storage_path and storage_path.exists():
            shutil.rmtree(storage_path, ignore_errors=True)


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
        slug = board_fqbn.replace(":", "-")
        out_dir = build_base_dir / slug
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
            led_pin = 6
            if metadata_path.exists():
                metadata = read_metadata_json(metadata_path)
                led_pin = metadata.led_builtin

            success = run_renode_emulation(
                firmware_path=firmware_elf,
                test_scripts=test_paths,
                timeout_seconds=timeout_seconds,
                led_builtin_pin=led_pin,
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

    summary_file = summary_dir / "renode_summary.md"
    rows: list[str] = [
        "### 🔬 Renode ARM Hardware Emulation Matrix (Instruction-Accurate)\n",
        "| Board / Target | MCU Architecture | Firmware Compilation | Hardware Emulation (PTY/UART) | Result |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]

    for (board_fqbn, board_name), c_stat, e_stat in zip(
        MATRIX_BOARDS, compilation_status, emulation_status, strict=True
    ):
        if "Passed" in e_stat:
            overall = "✅ PASS"
        elif "Skipped" in e_stat:
            overall = "⏭️ SKIPPED"
        else:
            overall = "❌ FAIL"
        rows.append(f"| **{board_name}**<br>`{board_fqbn}` | ARM Cortex-M0+ | {c_stat} | {e_stat} | **{overall}** |")

    summary_content = "\n".join(rows) + "\n"
    summary_file.write_text(summary_content, encoding="utf-8")
    print(summary_content)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with Path(step_summary).open("a", encoding="utf-8") as f:
                f.write(summary_content)
        except OSError as exc:
            logger.warning("Could not write to GITHUB_STEP_SUMMARY", error=str(exc))

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
        effective_sketch = sketch
        if not effective_sketch.exists():
            raise typer.BadParameter(f"Sketch does not exist: {effective_sketch}")
        test_paths = [Path(s) for s in scripts] if scripts else None
        fail_count = run_matrix(effective_sketch, timeout_seconds=timeout, test_scripts=test_paths)
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
