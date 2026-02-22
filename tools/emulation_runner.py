#!/usr/bin/env python3
"""
Hardware Emulation Runner.
This script is designed to launch SimAVR with the compiled Bridge firmware
and connect it via a virtual serial port (socat) to the Python McuBridge daemon.

It serves as the End-to-End test entrypoint.
"""

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # Graceful fallback if not installed, though CI should have it

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("emulation-runner")

SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
SOCAT_PORT1 = "/tmp/ttyBRIDGE1"
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883


class LogMonitor:
    """Captures and analyzes process output for success/failure signals."""

    def __init__(self, process, name):
        self.process = process
        self.name = name
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._reader_thread, daemon=True)
        self.thread.start()

        self.found_patterns = set()
        self.errors_detected = []

    def _reader_thread(self):
        # Read stdout and stderr combined if possible, or just one
        stream = self.process.stdout or self.process.stderr
        if not stream:
            return

        for line in iter(stream.readline, ""):
            if not line:
                break
            line_str = line.strip()
            if not line_str:
                continue

            # Echo to our logger for visibility
            logger.info(f"[{self.name}] {line_str}")

            # Analyze
            self._analyze_line(line_str)

            if self.stop_event.is_set():
                break

    def _analyze_line(self, line):
        # Success signals
        if "Serial transport established" in line:
            self.found_patterns.add("serial_connected")
        if "Connected to MQTT broker" in line:
            self.found_patterns.add("mqtt_connected")
        if "MCU link synchronised" in line:
            self.found_patterns.add("handshake_complete")

        # Failure signals
        lower_line = line.lower()
        if "traceback" in lower_line or "critical" in lower_line or "fatal" in lower_line:
            # Exclude known non-fatal warnings if any
            self.errors_detected.append(line)

    def stop(self):
        self.stop_event.set()

    def has_error(self):
        return len(self.errors_detected) > 0

    def check_success(self, pattern_key):
        return pattern_key in self.found_patterns


class MqttVerifier:
    """Verifies system state via MQTT."""

    def __init__(self):
        self.client = None
        self.sync_event = threading.Event()
        self.metrics_received = False
        self.connected = False

    def start(self):
        if not mqtt:
            logger.warning("paho-mqtt not available, skipping active MQTT verification")
            return

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        try:
            self.client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.client.loop_start()
        except Exception as e:
            logger.error(f"Failed to connect monitoring client to MQTT: {e}")

    def stop(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info("Monitor connected to MQTT broker")
            self.connected = True
            client.subscribe("br/system/bridge/handshake/value")
            client.subscribe("br/system/metrics")
        else:
            logger.error(f"Monitor MQTT connection failed: {reason_code}")

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic == "br/system/bridge/handshake/value":
                payload = json.loads(msg.payload)
                if payload.get("synchronised") is True:
                    if not self.sync_event.is_set():
                        logger.info("VERIFIED: Bridge reports synchronized state via MQTT")
                        self.sync_event.set()
            elif msg.topic == "br/system/metrics":
                self.metrics_received = True
        except Exception as e:
            logger.warning(f"Error parsing MQTT message: {e}")


def cleanup_process(proc, name):
    """Gracefully terminate a process."""
    if proc:
        if proc.poll() is None:
            logger.info(f"Terminating {name}...")
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning(f"{name} did not terminate, killing...")
                proc.kill()


def find_firmware(repo_root):
    """Locate the best firmware ELF for emulation."""
    base_build_path = repo_root / "openwrt-library-arduino/build"
    firmware_path = base_build_path / "BridgeControl/BridgeControl.ino.elf"

    if not firmware_path.exists():
        logger.warning(f"Firmware ELF not found at {firmware_path}")
        if base_build_path.exists():
            found_elfs = list(base_build_path.glob("**/*.elf"))
            # Prefer Mega variant for SimAVR atmega2560
            mega_elfs = [e for e in found_elfs if "mega" in str(e) or "2560" in str(e)]
            if mega_elfs:
                # Prefer BridgeControl for e2e
                preferred = [e for e in mega_elfs if "BridgeControl" in str(e)]
                firmware_path = preferred[0] if preferred else mega_elfs[0]
            elif found_elfs:
                firmware_path = found_elfs[0]

    if not firmware_path.exists():
        logger.error("CRITICAL: No valid firmware ELF found.")
        sys.exit(1)

    logger.info(f"Using firmware: {firmware_path}")
    return firmware_path


def start_socat():
    """Start socat to link daemon and MCU bridge."""
    logger.info("Starting socat (PTY link)...")
    socat_cmd = [
        "socat", "-d", "-d",
        f"pty,raw,echo=0,link={SOCAT_PORT0}",
        f"pty,raw,echo=0,link={SOCAT_PORT1}"
    ]
    socat_proc = subprocess.Popen(socat_cmd, stderr=subprocess.PIPE, text=True)
    socat_monitor = LogMonitor(socat_proc, "socat")

    timeout = 10
    start_time = time.time()
    while not (Path(SOCAT_PORT0).exists() and Path(SOCAT_PORT1).exists()):
        if time.time() - start_time > timeout:
            logger.error("Timeout waiting for socat PTYs")
            cleanup_process(socat_proc, "socat")
            sys.exit(1)
        time.sleep(0.1)

    logger.info(f"Virtual serial ports created: {SOCAT_PORT0} <-> {SOCAT_PORT1}")
    return socat_proc, socat_monitor


def run_bridge(simavr_proc, stop_event):
    """Bidirectional bridge worker thread."""
    import select
    logger.info("Bridge thread started.")
    try:
        with open(SOCAT_PORT1, "r+b", buffering=0) as pty:
            while not stop_event.is_set():
                if simavr_proc.poll() is not None:
                    logger.error("SimAVR died in bridge thread")
                    break

                r, _, _ = select.select([pty, simavr_proc.stdout], [], [], 0.1)

                if pty in r:
                    data = pty.read(1024)
                    if data:
                        # logger.info(f"[bridge] Daemon -> MCU: {data.hex().upper()}")
                        simavr_proc.stdin.write(data)
                        simavr_proc.stdin.flush()

                if simavr_proc.stdout in r:
                    data = simavr_proc.stdout.read(1024)
                    if data:
                        pty.write(data)
                        pty.flush()
                        try:
                            text = data.decode("utf-8", errors="ignore").strip()
                            if text and any(c.isalpha() for c in text):
                                logger.info(f"[simavr-out] {text}")
                        except Exception:
                            pass
    except Exception as e:
        logger.error(f"Bridge thread error: {e}")
    logger.info("Bridge thread stopping.")


def start_daemon(package_root, protocol):
    """Start the Python McuBridge daemon."""
    logger.info("Starting Bridge Daemon (Test Mode)...")
    daemon_env = os.environ.copy()
    os.makedirs("/tmp/mcubridge/spool", exist_ok=True)
    os.makedirs("/tmp/mcubridge/fs", exist_ok=True)

    shared_secret = "12345678901234567890123456789012"
    uci_config = {
        "serial_port": SOCAT_PORT0,
        "serial_baud": str(protocol.DEFAULT_BAUDRATE),
        "serial_shared_secret": shared_secret,
        "mqtt_host": MQTT_HOST,
        "mqtt_port": str(MQTT_PORT),
        "mqtt_tls": "0",
        "mqtt_spool_dir": "/tmp/mcubridge/spool",
        "file_system_root": "/tmp/mcubridge/fs",
        "watchdog_enabled": "0",
        "debug": "1",
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

    current_path = daemon_env.get("PYTHONPATH", "")
    daemon_env["PYTHONPATH"] = f"{uci_stub_dir.name}{os.pathsep}{str(package_root)}{os.pathsep}{current_path}"
    daemon_env["MCUBRIDGE_LOG_STREAM"] = "1"

    daemon_cmd = [sys.executable, "-u", "-m", "mcubridge.daemon", "--debug"]
    daemon_proc = subprocess.Popen(
        daemon_cmd,
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    return daemon_proc, uci_stub_dir


def main():
    """Main entrypoint for emulation test."""
    logger.info("Starting Emulation Runner (Strict Mode)...")

    for tool in ["simavr", "socat"]:
        if subprocess.call(["which", tool], stdout=subprocess.DEVNULL) != 0:
            logger.error(f"Required tool '{tool}' not found.")
            sys.exit(1)

    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "openwrt-mcu-bridge"
    sys.path.insert(0, str(package_root))
    from mcubridge.protocol import protocol

    firmware_path = find_firmware(repo_root)
    socat_proc, socat_monitor = start_socat()

    simavr_proc = None
    daemon_proc = None
    stop_bridge = threading.Event()

    try:
        logger.info(f"Starting simavr with {firmware_path}...")
        simavr_proc = subprocess.Popen(
            ["simavr", "-m", "atmega2560", "-f", "16000000", str(firmware_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )

        bridge_thread = threading.Thread(target=run_bridge, args=(simavr_proc, stop_bridge), daemon=True)
        bridge_thread.start()

        def _stderr_worker():
            for line in iter(simavr_proc.stderr.readline, b""):
                if not line:
                    break
                logger.info(f"[simavr-err] {line.decode('utf-8', errors='ignore').strip()}")

        threading.Thread(target=_stderr_worker, daemon=True).start()

        daemon_proc, uci_stub_dir = start_daemon(package_root, protocol)
        log_monitor = LogMonitor(daemon_proc, "daemon")
        mqtt_monitor = MqttVerifier()
        mqtt_monitor.start()

        max_wait = 30
        start_wait = time.time()
        success = False

        while time.time() - start_wait < max_wait:
            if daemon_proc.poll() is not None:
                logger.error(f"Daemon died with code {daemon_proc.returncode}")
                break
            if simavr_proc.poll() is not None:
                logger.error("SimAVR died unexpectedly")
                break
            if log_monitor.has_error():
                logger.error(f"Error in logs: {log_monitor.errors_detected[0]}")
                break

            if log_monitor.check_success("handshake_complete") and mqtt_monitor.sync_event.is_set():
                logger.info("SUCCESS: Log and MQTT both confirm synchronization.")
                success = True
                break
            time.sleep(0.5)

        if not success:
            logger.error("Emulation FAILED: Timeout waiting for synchronization.")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        stop_bridge.set()
        if 'mqtt_monitor' in locals():
            mqtt_monitor.stop()
        if 'log_monitor' in locals():
            log_monitor.stop()
        socat_monitor.stop()
        cleanup_process(daemon_proc, "daemon")
        cleanup_process(simavr_proc, "simavr")
        cleanup_process(socat_proc, "socat")
        if 'uci_stub_dir' in locals():
            uci_stub_dir.cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(main())
