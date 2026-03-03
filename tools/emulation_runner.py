#!/usr/bin/env python3
"""
Hardware Emulation Runner.
Improved version using 'subprocess' for core binary streams and 'sh' for CLI scripts.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import threading
import errno
import select
from pathlib import Path
from typing import Any, Annotated

import typer

try:
    import sh
except ImportError:
    sh = None  # type: ignore

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("emulation-runner")

app = typer.Typer(help="Hardware Emulation Runner.")

SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
SOCAT_PORT1 = "/tmp/ttyBRIDGE1"
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883


class EmulationState:
    """Track patterns and errors in process output."""

    def __init__(self):
        self.found_patterns = set()
        self.errors_detected = []

    def on_line(self, line: str, name: str):
        line = line.strip()
        if not line:
            return
        logger.info("[%s] %s", name, line)

        # Success signals
        if "Serial transport established" in line:
            self.found_patterns.add("serial_connected")
        if "Connected to MQTT broker" in line:
            self.found_patterns.add("mqtt_connected")
        if "MCU link synchronised" in line or '"message":"MCU link synchronised' in line:
            self.found_patterns.add("handshake_complete")

        # Failure signals
        lower = line.lower()
        if "traceback" in lower or "critical" in lower or "fatal" in lower:
            if "_on_subscribe" not in line and "Unexpected message ID" not in line:
                self.errors_detected.append(line)

    def has_error(self) -> bool:
        return len(self.errors_detected) > 0

    def check_success(self, key: str) -> bool:
        return key in self.found_patterns


class MqttVerifier:
    """Verifies system state via MQTT."""

    def __init__(self):
        self.client = None
        self.sync_event = threading.Event()
        self.metrics_received = False
        self.connected = False

    def start(self):
        if not mqtt:
            return
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        try:
            self.client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.client.loop_start()
        except Exception:
            pass

    def stop(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self.connected = True
            client.subscribe("br/system/bridge/handshake/value")
            client.subscribe("br/system/metrics")

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic == "br/system/bridge/handshake/value":
                payload = json.loads(msg.payload)
                if payload.get("synchronised") is True:
                    self.sync_event.set()
            elif msg.topic == "br/system/metrics":
                self.metrics_received = True
        except Exception:
            pass


def _setup_uci_and_env(
    package_root: Path, shared_secret: str
) -> tuple[dict[str, str], tempfile.TemporaryDirectory[str]]:
    """Setup UCI stub and return daemon environment."""
    os.makedirs("/tmp/mcubridge/spool", exist_ok=True)
    os.makedirs("/tmp/mcubridge/fs", exist_ok=True)

    uci_config = {
        "serial_port": SOCAT_PORT0,
        "serial_baud": "115200",
        "serial_shared_secret": shared_secret,
        "mqtt_host": MQTT_HOST,
        "mqtt_port": str(MQTT_PORT),
        "mqtt_tls": "0",
        "mqtt_spool_dir": "/tmp/mcubridge/spool",
        "file_system_root": "/tmp/mcubridge/fs",
        "watchdog_enabled": "0",
        "debug": "1",
        "allowed_commands": "*",
    }
    uci_stub_dir = tempfile.TemporaryDirectory(prefix="mcubridge-uci-")
    uci_stub_path = Path(uci_stub_dir.name) / "uci.py"
    uci_stub_path.write_text(
        textwrap.dedent(
            """\
            from __future__ import annotations
            _CONFIG = {config!r}
            class Uci:
                def __enter__(self): return self
                def __exit__(self, exc_type, exc, tb): return False
                def get_all(self, package: str, section: str):
                    if package == "mcubridge" and section == "general":
                        return dict(_CONFIG)
                    return None
            """
        ).format(config=uci_config),
        encoding="utf-8",
    )

    env = os.environ.copy()
    current_path = env.get("PYTHONPATH", "")
    repo_root = package_root.parent
    client_examples = repo_root / "mcubridge-client-examples"
    path_parts = [uci_stub_dir.name, str(package_root), str(client_examples), current_path]
    env["PYTHONPATH"] = os.pathsep.join(p for p in path_parts if p)
    env["MCUBRIDGE_LOG_STREAM"] = "1"
    env["COLUMNS"] = "500"  # Prevent Rich from wrapping log lines in piped output
    return env, uci_stub_dir


def _start_worker_thread(target: Any, name: str, *args) -> None:
    t = threading.Thread(target=target, name=name, args=args, daemon=True)
    t.start()


def _socat_worker(socat_proc: subprocess.Popen, state: EmulationState) -> None:
    if socat_proc.stderr:
        for line in iter(socat_proc.stderr.readline, ""):
            if not line:
                break
            state.on_line(line, "socat")

def _mcu_stderr_worker(mcu_proc: subprocess.Popen, state: EmulationState) -> None:
    if mcu_proc.stderr:
        for line in iter(mcu_proc.stderr.readline, b""):
            if not line:
                break
            state.on_line(line.decode("utf-8", errors="ignore"), "mcu-err")

def _daemon_worker(daemon_proc: subprocess.Popen, state: EmulationState) -> None:
    if daemon_proc.stdout:
        for line in iter(daemon_proc.stdout.readline, ""):
            if not line:
                break
            state.on_line(line, "daemon")

def _serial_bridge_worker(
    mcu_proc: subprocess.Popen, state: EmulationState, serial_bridge_ready: threading.Event
) -> None:
    pty_open_timeout_s = 10.0
    start = time.monotonic()
    mcu_out = mcu_proc.stdout
    mcu_in = mcu_proc.stdin
    if mcu_out is None or mcu_in is None:
        state.errors_detected.append("serial bridge aborted: missing MCU stdio")
        return

    out_fd = mcu_out.fileno()
    in_fd = mcu_in.fileno()

    while mcu_proc.poll() is None:
        pty_handle = None
        while mcu_proc.poll() is None and pty_handle is None:
            try:
                pty_handle = open(SOCAT_PORT1, "r+b", buffering=0)
                serial_bridge_ready.set()
            except OSError:
                if time.monotonic() - start >= pty_open_timeout_s:
                    message = f"Serial bridge failed: {SOCAT_PORT1} was not ready in {pty_open_timeout_s:.1f}s"
                    logger.error(message)
                    state.errors_detected.append(message)
                    return
                time.sleep(0.05)

        if pty_handle is None:
            message = "Serial bridge aborted: MCU process exited before PTY was ready."
            logger.error(message)
            state.errors_detected.append(message)
            return

        try:
            with pty_handle as pty:
                pty_fd = pty.fileno()
                while mcu_proc.poll() is None:
                    r, _, _ = select.select([pty_fd, out_fd], [], [], 0.05)
                    if pty_fd in r:
                        data = os.read(pty_fd, 1024)
                        if data:
                            os.write(in_fd, data)
                    if out_fd in r:
                        data = os.read(out_fd, 1024)
                        if data:
                            os.write(pty_fd, data)
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                logger.warning("Serial bridge transient PTY error (%s). Reopening %s...", e, SOCAT_PORT1)
                time.sleep(0.05)
                continue
            message = f"Serial bridge I/O failure: {e}"
            logger.error(message)
            state.errors_detected.append(message)
            return
        except Exception as e:
            message = f"Serial bridge thread exiting: {e}"
            logger.error(message)
            state.errors_detected.append(message)
            return

def _start_socat_process(state: EmulationState) -> subprocess.Popen[Any]:
    logger.info("Starting socat...")
    socat_proc = subprocess.Popen(
        ["socat", "-d", "-d", f"pty,raw,echo=0,link={SOCAT_PORT0}", f"pty,raw,echo=0,link={SOCAT_PORT1}"],
        stderr=subprocess.PIPE,
        text=True,
    )
    _start_worker_thread(_socat_worker, "socat-worker", socat_proc, state)
    return socat_proc

def _start_mcu_process(firmware_path: Path, state: EmulationState) -> tuple[subprocess.Popen[Any], threading.Event]:
    logger.info("Starting MCU Emulator...")
    mcu_proc = subprocess.Popen(
        [str(firmware_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    serial_bridge_ready = threading.Event()
    _start_worker_thread(_serial_bridge_worker, "serial-bridge", mcu_proc, state, serial_bridge_ready)
    _start_worker_thread(_mcu_stderr_worker, "mcu-stderr-worker", mcu_proc, state)
    return mcu_proc, serial_bridge_ready

def _start_daemon_process(daemon_env: dict[str, str], state: EmulationState) -> subprocess.Popen[Any]:
    logger.info("Starting Daemon...")
    daemon_proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "mcubridge.daemon", "--debug"],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    _start_worker_thread(_daemon_worker, "daemon-worker", daemon_proc, state)
    return daemon_proc

def setup_emulation_processes(
    state: EmulationState,
    firmware_path: Path,
    daemon_env: dict[str, str],
) -> tuple[subprocess.Popen[Any], subprocess.Popen[Any], subprocess.Popen[Any]]:
    """Start socat, mcu and daemon processes with their workers."""
    socat_proc = _start_socat_process(state)
    mcu_proc, serial_bridge_ready = _start_mcu_process(firmware_path, state)

    if not serial_bridge_ready.wait(timeout=10.0):
        message = f"Serial bridge was not ready before daemon startup ({SOCAT_PORT1})"
        logger.error(message)
        state.errors_detected.append(message)

    daemon_proc = _start_daemon_process(daemon_env, state)

    return socat_proc, mcu_proc, daemon_proc


def _run_client_scripts(scripts: list[str], env: dict[str, str]) -> bool:
    """Run client example scripts and return True if all passed."""
    success = True
    for script in scripts:
        logger.info("Running script: %s", script)
        try:
            cmd = [
                sys.executable,
                script,
                "--host",
                MQTT_HOST,
                "--port",
                str(MQTT_PORT),
                "--user",
                "admin",
                "--password",
                "admin",
            ]
            subprocess.run(cmd, env=env, check=True, timeout=30, stdin=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error("Script failed: %s", e)
            success = False
    return success


def run_emulation(
    state: EmulationState,
    mqtt_verifier: MqttVerifier,
    firmware_path: Path,
    package_root: Path,
    run_scripts: list[str] | None = None,
) -> bool:
    """Orchestrate the emulation lifecycle."""
    daemon_env, uci_dir = _setup_uci_and_env(package_root, "DEBUG_INSECURE")
    socat_proc, mcu_proc, daemon_proc = setup_emulation_processes(state, firmware_path, daemon_env)

    # Wait for PTYs
    for _ in range(50):
        if Path(SOCAT_PORT0).exists() and Path(SOCAT_PORT1).exists():
            break
        time.sleep(0.1)
    else:
        logger.error("Socat timeout")
        socat_proc.terminate()
        return False

    mqtt_verifier.start()
    success = False
    try:
        start_time = time.time()
        # Increased timeout for handshake in CI environments
        while time.time() - start_time < 45:
            if state.check_success("handshake_complete") or mqtt_verifier.sync_event.is_set():
                logger.info("SUCCESS: Handshake verified.")
                success = True
                break
            if daemon_proc.poll() is not None or mcu_proc.poll() is not None:
                logger.error("Process died prematurely")
                break
            time.sleep(0.5)

        if success and run_scripts:
            success = _run_client_scripts(run_scripts, daemon_env)
    finally:
        mqtt_verifier.stop()
        daemon_proc.terminate()
        mcu_proc.terminate()
        socat_proc.terminate()
        uci_dir.cleanup()

    return success


@app.command()
def main(
    run_scripts: Annotated[list[str] | None, typer.Argument(help="Scripts to run")] = None,
    firmware: Annotated[str, typer.Option(help="Emulator binary name")] = "bridge_emulator",
) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "mcubridge"
    firmware_path = repo_root / f"mcubridge-library-arduino/tests/{firmware}"

    if not firmware_path.exists():
        logger.error("Firmware not found at %s", firmware_path)
        raise typer.Exit(1)

    state = EmulationState()
    mqtt_verifier = MqttVerifier()

    success = run_emulation(
        state=state,
        mqtt_verifier=mqtt_verifier,
        firmware_path=firmware_path,
        package_root=package_root,
        run_scripts=run_scripts,
    )

    if not success:
        logger.error("Emulation FAILED.")
        sys.exit(1)


if __name__ == "__main__":
    app()
