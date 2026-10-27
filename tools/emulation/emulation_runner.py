#!/usr/bin/env python3
"""[MIL-SPEC/SIL-2] Canonical Hardware Emulation Runner & Shared Infrastructure.

Direct PTY-PTY link via socat, with MCU opening its PTY directly.
Provides canonical shared primitives (EmulationState, gateway lifecycle, fake UCI
environment setup, in-process client script execution, post-execution status audit,
and matrix summary generation) for simavr, Renode, QEMU, and fleet mesh runners.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, cast

import structlog
import tenacity
import typer
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import protocol
from tools.arduino_core_metadata import read_metadata_json
from tools.audit.audit_bridge_status import audit_status_dict
from tools.emulation.process_utils import (
    cleanup_paths,
    start_daemon_thread,
    stream_pump,
    terminate_process_tree,
    wait_for_path_ready,
    wait_for_tcp_ready,
    write_fake_uci_module,
)

repo_root = Path(__file__).resolve().parents[2]

# --- Constants ---
SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = protocol.DEFAULT_CLOUD_PORT
DEFAULT_SHARED_SECRET = "8c6ecc8216447ee1525c0743737f3a5c0eef0c03a045ab50e5ea95687e826ebe"
QEMU_LISTEN_MARKER = "QEMU waiting for connection"
CORE_METADATA_PATH = repo_root / "build" / "emulation" / "arduino_core_metadata.json"

configure_logging(console=True)
logger = structlog.get_logger("emulation-runner")


def default_output_lines() -> list[tuple[str, str]]:
    return []


def default_client_test_paths() -> list[Path]:
    return [
        repo_root / "mcubridge-client-examples" / "tests" / "test_smoke_connection.py",
        repo_root / "mcubridge-client-examples" / "examples" / "led13_test.py",
        repo_root / "mcubridge-client-examples" / "examples" / "console_test.py",
        repo_root / "mcubridge-client-examples" / "examples" / "mailbox_read_test.py",
    ]


@dataclass
class EmulationState:
    """Canonical thread-safe log collector and lifecycle synchronization state."""

    output_lines: list[tuple[str, str]] = field(default_factory=default_output_lines)
    lock: threading.Lock = field(default_factory=threading.Lock)
    sync_event: threading.Event = field(default_factory=threading.Event)
    capabilities_event: threading.Event = field(default_factory=threading.Event)
    listening_event: threading.Event = field(default_factory=threading.Event)

    def on_line(self, line: str, source: str) -> None:
        if source == "qemu" and QEMU_LISTEN_MARKER in line:
            self.listening_event.set()
        clean_line = line.strip()
        if not clean_line:
            return
        with self.lock:
            self.output_lines.append((source, clean_line))
            logger.info("Process output", source=source, line=clean_line)
            if "Capabilities discovery complete" in clean_line or "MCU capabilities received" in clean_line:
                self.capabilities_event.set()
                self.sync_event.set()
            elif (
                "MCU link synchronised" in clean_line
                or "MCU link synchronized" in clean_line
                or "Handshake synchronization complete" in clean_line
                or '"new_state": "SYNCHRONIZED"' in clean_line
            ):
                self.sync_event.set()


def ensure_cloud_gateway(state: EmulationState) -> subprocess.Popen[str] | None:
    """Start Managed Cloud Gateway if not already listening (Rule 2.1 / Rule 19)."""
    gateway_proc: subprocess.Popen[str] | None = None
    if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=1.0):
        logger.info("Starting Managed Cloud Gateway...")
        gateway_env = dict(os.environ)
        gateway_env["PYTHONUNBUFFERED"] = "1"
        gateway_cmd = [
            sys.executable,
            "-u",
            str(repo_root / "mcubridge-gateway" / "gateway.py"),
            "--no-tls",
            "--port",
            str(CLOUD_PORT),
        ]
        gateway_proc = subprocess.Popen(
            gateway_cmd,
            env=gateway_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "gateway", gateway_proc.stdout, state.on_line, "gateway")

    if not wait_for_tcp_ready(CLOUD_HOST, CLOUD_PORT, timeout=30.0):
        logger.error("Cloud Gateway not available after spawn")
        if gateway_proc is not None:
            terminate_process_tree([gateway_proc], timeout=1.0)
        return None

    return gateway_proc


def prepare_emulator_fs(fs_root: Path) -> None:
    """Ensure emulator filesystem root directory is clean and prepared."""
    if fs_root.exists():
        try:
            shutil.rmtree(fs_root)
        except OSError as exc:
            logger.error("Failed to clean emulator FS root", path=str(fs_root), error=str(exc))
    fs_root.mkdir(parents=True, exist_ok=True)


def setup_fake_uci(
    slave_name: str,
    *,
    device_id: str = "yun-01",
    shared_secret: str = DEFAULT_SHARED_SECRET,
    base_dir: Path | None = None,
    status_file: Path | None = None,
    emulator_fs_root: Path | None = None,
    p_root: Path = repo_root,
    label: str = "emulation runner",
) -> tuple[Path, Path, dict[str, str]]:
    """Canonical fake UCI directory, LMDB storage, and daemon environment builder (Rule 19)."""
    spool_path: Path | None = None
    if base_dir is not None:
        fake_uci_dir = base_dir / "uci"
        fake_uci_dir.mkdir(parents=True, exist_ok=True)
        storage_path = base_dir / "storage"
        storage_path.mkdir(parents=True, exist_ok=True)
        spool_dir = base_dir / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        spool_path = spool_dir
        if emulator_fs_root is None:
            emulator_fs_root = base_dir / "fs"
            emulator_fs_root.mkdir(parents=True, exist_ok=True)
    else:
        fake_uci_dir = Path(tempfile.mkdtemp(prefix="mcubridge_uci_"))
        storage_path = Path(tempfile.mkdtemp(prefix="mcubridge_db_"))

    uci_config: dict[str, str] = {
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
        "serial_shared_secret": shared_secret,
        "allowed_commands": "*",
        "storage_path": str(storage_path),
        "debug": "1",
    }
    if spool_path is not None:
        uci_config["cloud_spool_dir"] = str(spool_path)
    if emulator_fs_root is not None:
        uci_config["file_system_root"] = str(emulator_fs_root)
    write_fake_uci_module(fake_uci_dir, uci_config, label=label)

    daemon_env = dict(os.environ)
    extra_paths = [
        str(fake_uci_dir),
        str(p_root / "mcubridge"),
        str(p_root / "mcubridge-client-examples"),
        str(p_root),
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
    daemon_env["MCUBRIDGE_SERIAL_SHARED_SECRET"] = shared_secret
    daemon_env["MCUBRIDGE_DISABLE_METRICS"] = "1"
    daemon_env["MCUBRIDGE_STORAGE_PATH"] = str(storage_path)
    daemon_env["MCUBRIDGE_CLOUD_ENABLED"] = "1"
    daemon_env["MCUBRIDGE_CLOUD_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_CLOUD_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_GATEWAY_HOST"] = CLOUD_HOST
    daemon_env["MCUBRIDGE_GATEWAY_PORT"] = str(CLOUD_PORT)
    daemon_env["MCUBRIDGE_DEVICE_ID"] = device_id
    daemon_env["MCUBRIDGE_DEBUG"] = "1"
    if status_file is not None:
        daemon_env["MCUBRIDGE_STATUS_FILE"] = str(status_file)
    if spool_path is not None:
        daemon_env["MCUBRIDGE_CLOUD_SPOOL_DIR"] = str(spool_path)
    if emulator_fs_root is not None:
        daemon_env["MCUBRIDGE_FILE_SYSTEM_ROOT"] = str(emulator_fs_root)

    return fake_uci_dir, storage_path, daemon_env


def wait_for_daemon_sync(status_file: Path, timeout: float = 15.0) -> bool:
    """Wait for Daemon/MCU synchronization using declarative tenacity retry."""

    def _is_synced() -> bool:
        if not status_file.exists():
            return False
        try:
            raw_obj = json.loads(status_file.read_text(encoding="utf-8"))
            if not isinstance(raw_obj, dict):
                return False
            data = cast(dict[str, object], raw_obj)
            metrics_obj = data.get("metrics")
            if isinstance(metrics_obj, dict):
                metrics = cast(dict[str, object], metrics_obj)
                if metrics.get("link_state") == "connected" and metrics.get("handshake_state") == "synchronized":
                    return True
            bridge_obj = data.get("bridge")
            if isinstance(bridge_obj, dict):
                bridge = cast(dict[str, object], bridge_obj)
                if bridge.get("is_synchronized") is True or bridge.get("synchronised") is True:
                    return True
                serial_obj = bridge.get("serial_link")
                if isinstance(serial_obj, dict):
                    serial_link = cast(dict[str, object], serial_obj)
                    if serial_link.get("synchronised") is True:
                        return True
                handshake_obj = bridge.get("handshake")
                if isinstance(handshake_obj, dict):
                    handshake = cast(dict[str, object], handshake_obj)
                    if handshake.get("synchronised") is True:
                        return True
            return False
        except (json.JSONDecodeError, OSError):
            return False

    sync_retryer = tenacity.Retrying(
        stop=tenacity.stop_after_delay(timeout),
        wait=tenacity.wait_fixed(0.25),
        retry=tenacity.retry_if_result(lambda ok: not ok),
        reraise=False,
    )
    try:
        return sync_retryer(_is_synced)
    except tenacity.RetryError:
        return False


def run_client_script(
    script_path: Path,
    device_id: str = "yun-01",
    led_builtin_pin: int | None = None,
) -> bool:
    """Execute client test script directly in-process via module main or runpy (Rule 19 / Rule 37)."""
    logger.info("Executing client test in-process", script=script_path.name)
    try:
        spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[script_path.stem] = mod
            spec.loader.exec_module(mod)
            main_fn = getattr(mod, "main", None)
            if callable(main_fn):
                main_parameters = inspect.signature(main_fn).parameters
                kwargs: dict[str, object] = {}
                if "host" in main_parameters:
                    kwargs["host"] = CLOUD_HOST
                if "port" in main_parameters:
                    kwargs["port"] = CLOUD_PORT
                if "device_id" in main_parameters:
                    kwargs["device_id"] = device_id
                if "led_builtin_pin" in main_parameters:
                    if led_builtin_pin is None:
                        raise ValueError(f"{script_path.name} requires LED_BUILTIN from its Arduino core")
                    kwargs["led_builtin_pin"] = led_builtin_pin
                main_fn(**kwargs)
                logger.info("Script execution passed", script=script_path.name)
                return True

        orig_argv = sys.argv[:]
        try:
            sys.argv = [str(script_path), "--device-id", device_id]
            runpy.run_path(str(script_path), run_name="__main__")
        finally:
            sys.argv = orig_argv

        logger.info("Script execution passed", script=script_path.name)
        return True
    except SystemExit as exc:
        if exc.code in (0, None):
            logger.info("Script execution passed", script=script_path.name)
            return True
        logger.error("Script exited with error", script=script_path.name, code=exc.code)
        return False
    except (OSError, RuntimeError, ValueError, TypeError, AssertionError) as exc:
        logger.error("Script execution failed", script=script_path.name, error=str(exc))
        return False


def run_client_scripts(
    test_scripts: Sequence[Path],
    *,
    device_id: str = "yun-01",
    led_builtin_pin: int | None = None,
) -> bool:
    """Execute a sequence of client test scripts with MCUBRIDGE_NON_INTERACTIVE=1 (Rule 19)."""
    old_non_interactive = os.environ.get("MCUBRIDGE_NON_INTERACTIVE")
    os.environ["MCUBRIDGE_NON_INTERACTIVE"] = "1"
    try:
        for test_path in test_scripts:
            if not test_path.exists():
                logger.warning("Test script not found, skipping", path=str(test_path))
                continue
            if not run_client_script(test_path, device_id=device_id, led_builtin_pin=led_builtin_pin):
                return False
        return True
    finally:
        if old_non_interactive is None:
            os.environ.pop("MCUBRIDGE_NON_INTERACTIVE", None)
        else:
            os.environ["MCUBRIDGE_NON_INTERACTIVE"] = old_non_interactive


def audit_post_execution_status(status_file: Path = Path("/tmp/mcubridge_status.json")) -> bool:
    """Audit runtime status snapshot and active system health (SIL-2 / Rule 29)."""
    if not status_file.exists():
        logger.warning("Status file does not exist for post-execution audit", path=str(status_file))
        return True

    try:
        status_data = json.loads(status_file.read_text(encoding="utf-8"))
        if not isinstance(status_data, dict):
            logger.error("Status file root is not a dictionary")
            return False

        status_errors = audit_status_dict(cast(dict[str, Any], status_data))
        if status_errors:
            logger.error("Post-execution status health check failed", errors=status_errors)
            for err in status_errors:
                logger.error("Status audit anomaly", error=err)
            return False

        logger.info("Post-execution status health check passed (100% clean)")
        return True
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error("Failed auditing bridge status", error=str(exc))
        return False


def write_matrix_summary(
    summary_dir: Path,
    *,
    prefix: str,
    title: str,
    engine_label: str,
    architecture: str,
    boards: Sequence[tuple[str, str]],
    compilation_status: Sequence[str],
    emulation_status: Sequence[str],
    mirror_dir: Path | None = None,
) -> None:
    """Write canonical Markdown + JSON hardware emulation matrix summary (Rule 19)."""
    summary_file = summary_dir / f"{prefix}_summary.md"
    summary_json_file = summary_dir / f"{prefix}_summary.json"
    rows: list[str] = [
        f"### 🔬 {title}\n",
        "| Board / Target | MCU Architecture | Firmware Compilation | Hardware Emulation (PTY/UART) | Result |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]

    boards_data: list[dict[str, str]] = []
    for (board_fqbn, board_name), c_stat, e_stat in zip(boards, compilation_status, emulation_status, strict=True):
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
    summary_file.write_text(summary_content, encoding="utf-8")
    summary_json_file.write_text(
        json.dumps({"engine": engine_label, "boards": boards_data}, indent=2),
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

    if mirror_dir is not None and mirror_dir.exists():
        for src_file in (summary_file, summary_json_file):
            target_path = mirror_dir / src_file.name
            if src_file.resolve() != target_path.resolve():
                shutil.copy2(src_file, target_path)


def run_emulation(
    firmware_path: Path,
    package_root: Path = Path(),
    run_scripts: list[str] | None = None,
    led_builtin_pin: int | None = None,
) -> None:
    state = EmulationState()
    gateway_proc = ensure_cloud_gateway(state)

    Path(SOCAT_PORT0).unlink(missing_ok=True)
    status_file = Path("/tmp/mcubridge_status.json")
    status_file.unlink(missing_ok=True)
    os.environ["MCUBRIDGE_LOG_STREAM"] = "1"

    emulator_fs_root = Path("/tmp/mcubridge-host-fs")
    prepare_emulator_fs(emulator_fs_root)

    logger.info("Starting Unified MCU Emulator via socat EXEC...")
    mcu_proc = subprocess.Popen(
        [
            "socat",
            "-d",
            "-d",
            f"PTY,link={SOCAT_PORT0},raw,echo=0",
            f"EXEC:{firmware_path.absolute()},pty,raw,echo=0",
        ],
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    start_daemon_thread(stream_pump, "mcu-socat", mcu_proc.stderr, state.on_line, "mcu")

    if not wait_for_path_ready(SOCAT_PORT0, timeout=10.0, interval=0.1):
        logger.error("Timeout waiting for unified PTY", path=SOCAT_PORT0)
        terminate_process_tree([mcu_proc], timeout=1.0)
        sys.exit(1)

    p_root = package_root.absolute()
    fake_uci_dir, storage_path, daemon_env = setup_fake_uci(
        SOCAT_PORT0,
        device_id="yun-01",
        shared_secret="DEBUG_INSECURE",
        emulator_fs_root=emulator_fs_root,
        p_root=p_root,
        label="e2e runner",
    )
    daemon_cmd = [sys.executable, "-u"]
    if os.environ.get("COVERAGE_FILE"):
        daemon_cmd.extend(["-m", "coverage", "run", "--append", "--rcfile", str(p_root / "pyproject.toml")])
    daemon_cmd.extend(["-m", "mcubridge.daemon"])
    daemon_proc = None
    all_success = True

    try:
        logger.info("Starting Daemon...")
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            env=daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        start_daemon_thread(stream_pump, "daemon", daemon_proc.stdout, state.on_line, "daemon")

        logger.info("Waiting for Daemon/MCU synchronization...")
        if not wait_for_daemon_sync(status_file, timeout=15.0):
            raise RuntimeError("Synchronization check timed out after 15s; aborting test execution.")
        logger.info("Daemon/MCU synchronization established successfully.")

        if run_scripts:
            for script in run_scripts:
                s_path = Path(script)
                if not s_path.exists():
                    logger.error("Script not found", script=script)
                    all_success = False
                    break

                with state.lock:
                    lines_before = len(state.output_lines)

                if not run_client_script(s_path, device_id="yun-01", led_builtin_pin=led_builtin_pin):
                    all_success = False
                    break

                with state.lock:
                    new_lines = list(state.output_lines[lines_before:])
                script_errors = [
                    f"[{src}] {line}"
                    for src, line in new_lines
                    if '"level": "error"' in line or '"level":"error"' in line or "MCU > ERROR:" in line
                ]
                if script_errors:
                    logger.error(
                        "Script produced unexpected error log events",
                        script=script,
                        error_count=len(script_errors),
                    )
                    for err in script_errors:
                        logger.error("Unexpected script error", detail=err)
                    all_success = False
                    break

                time.sleep(1)

        if all_success and not audit_post_execution_status(status_file):
            all_success = False
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Emulation error", error=str(exc))
        all_success = False
    finally:
        procs_to_terminate: list[subprocess.Popen[Any]] = [
            p for p in (daemon_proc, mcu_proc, gateway_proc) if p is not None
        ]
        terminate_process_tree(procs_to_terminate, timeout=2.0)
        cleanup_paths(fake_uci_dir, storage_path)

    if not all_success:
        logger.error("Emulation FAILED.")
        sys.exit(1)
    logger.info("Emulation SUCCESS.")


cli = typer.Typer(help="Hardware Emulation Runner", add_completion=False)


@cli.command()
def main(
    firmware: Annotated[Path, typer.Option("--firmware", help="Path to MCU firmware binary")],
    package_root: Annotated[Path, typer.Option("--package-root", help="Root of mcubridge package")] = Path(),
    run_scripts: Annotated[list[str] | None, typer.Argument(help="Client scripts to run")] = None,
) -> None:
    os.environ["MCUBRIDGE_NON_INTERACTIVE"] = "1"
    scripts = run_scripts or []
    led_builtin_pin = None
    if any(Path(script).name in {"led13_test.py", "all_features_test.py"} for script in scripts):
        led_builtin_pin = read_metadata_json(CORE_METADATA_PATH).led_builtin
    run_emulation(
        firmware_path=firmware,
        package_root=package_root,
        run_scripts=scripts,
        led_builtin_pin=led_builtin_pin,
    )


if __name__ == "__main__":
    cli()
