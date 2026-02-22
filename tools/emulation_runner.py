#!/usr/bin/env python3
"""
Hardware Emulation Runner.
This script is designed to launch SimAVR with the compiled Bridge firmware
and connect it via a virtual serial port (socat) to the Python McuBridge daemon.

It serves as the End-to-End test entrypoint.
"""

import logging
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import time
import json
import threading
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
    if proc:
        if proc.poll() is None:
            logger.info(f"Terminating {name}...")
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning(f"{name} did not terminate, killing...")
                proc.kill()


def main():
    logger.info("Starting Emulation Runner (Strict Mode)...")

    # 1. Check for required tools
    required_tools = ["simavr", "socat"]
    for tool in required_tools:
        if subprocess.call(["which", tool], stdout=subprocess.DEVNULL) != 0:
            logger.error(f"Required tool '{tool}' not found.")
            sys.exit(0)

    # 2. Paths
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    package_root = repo_root / "openwrt-mcu-bridge"
    sys.path.insert(0, str(package_root))
    from mcubridge.protocol import protocol

    # Path to Firmware
    base_build_path = repo_root / "openwrt-library-arduino/build"
    firmware_path = base_build_path / "BridgeControl/BridgeControl.ino.elf"

    if not firmware_path.exists():
        logger.warning(f"Firmware ELF not found at {firmware_path}")
        if base_build_path.exists():
            logger.info("Available ELFs in build dir:")
            found_elfs = list(base_build_path.glob("**/*.elf"))
            for elf in found_elfs:
                logger.info(f" - {elf}")
                if "BridgeControl" in str(elf) and not firmware_path.exists():
                    firmware_path = elf
                    logger.info(f"Auto-selected firmware: {firmware_path}")

    if not firmware_path.exists():
        logger.error("CRITICAL: No valid firmware ELF found.")
        sys.exit(1)

    # 3. Setup Virtual Serial Port
    logger.info("Starting socat...")
    socat_cmd = ["socat", "-d", "-d", f"pty,raw,echo=0,link={SOCAT_PORT0}", f"pty,raw,echo=0,link={SOCAT_PORT1}"]
    socat_proc = subprocess.Popen(socat_cmd, stderr=subprocess.PIPE, text=True)

    timeout = 5
    start_time = time.time()
    while not (Path(SOCAT_PORT0).exists() and Path(SOCAT_PORT1).exists()):
        if time.time() - start_time > timeout:
            logger.error("Timeout waiting for socat PTYs")
            cleanup_process(socat_proc, "socat")
            sys.exit(1)
        time.sleep(0.1)

    logger.info(f"Virtual serial ports created: {SOCAT_PORT0} <-> {SOCAT_PORT1}")

    simavr_proc = None
    daemon_proc = None
    log_monitor = None
    mqtt_monitor = None

    try:
        # 4. Start SimAVR
        logger.info(f"Starting simavr with {firmware_path}...")
        simavr_cmd = ["simavr", "-m", "atmega2560", "-f", "16000000", str(firmware_path)]
        simavr_proc = subprocess.Popen(simavr_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) # Reduce noise

        # 5. Start Python Daemon (Test Mode)
        logger.info("Starting Bridge Daemon (Test Mode)...")
        daemon_env = os.environ.copy()
        os.makedirs("/tmp/mcubridge/spool", exist_ok=True)
        os.makedirs("/tmp/mcubridge/fs", exist_ok=True)

        uci_config = {
            "serial_port": SOCAT_PORT0,
            "serial_baud": str(protocol.DEFAULT_BAUDRATE),
            "serial_shared_secret": "emulation_test_secret_xyz",
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

        current_pythonpath = daemon_env.get("PYTHONPATH", "")
        daemon_env["PYTHONPATH"] = f"{uci_stub_dir.name}{os.pathsep}{str(package_root)}{os.pathsep}{current_pythonpath}"
        daemon_env["MCUBRIDGE_LOG_STREAM"] = "1" # Force stdout logs

        # Capture both stdout and stderr for analysis
        daemon_cmd = [sys.executable, "-u", "-m", "mcubridge.daemon", "--debug"]
        daemon_proc = subprocess.Popen(
            daemon_cmd,
            env=daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr to stdout
            text=True,
            bufsize=1 # Line buffered
        )

        # Start Monitors
        log_monitor = LogMonitor(daemon_proc, "daemon")
        mqtt_monitor = MqttVerifier()
        mqtt_monitor.start()

        # 6. Strict Verification Loop
        # We wait up to 20 seconds for the system to reach 'Synchronized' state
        max_wait = 20
        start_wait = time.time()
        success = False

        logger.info("Waiting for synchronization (Timeout: 20s)...")

        while time.time() - start_wait < max_wait:
            # Check for process death
            if daemon_proc.poll() is not None:
                logger.error(f"Daemon died unexpectedly with code {daemon_proc.returncode}")
                break
            if simavr_proc.poll() is not None:
                logger.error(f"SimAVR died unexpectedly with code {simavr_proc.returncode}")
                break

            # Check for critical log errors
            if log_monitor.has_error():
                logger.error(f"Critical error detected in logs: {log_monitor.errors_detected[0]}")
                break

            # Check Success Conditions
            # 1. Log Confirmation
            log_sync = log_monitor.check_success("handshake_complete")
            # 2. MQTT Confirmation (Truth)
            mqtt_sync = mqtt_monitor.sync_event.is_set()

            if log_sync and mqtt_sync:
                logger.info("SUCCESS: Log and MQTT both confirm synchronization.")
                success = True
                break

            time.sleep(0.5)

        if success:
            logger.info("Emulation test run completed successfully (Strict Mode).")
        else:
            logger.error("Emulation FAILED: Timeout waiting for synchronization or process failure.")
            logger.info(f"Log Sync Detected: {log_monitor.check_success('handshake_complete')}")
            logger.info(f"MQTT Sync Detected: {mqtt_monitor.sync_event.is_set()}")
            logger.info(f"MQTT Connected: {mqtt_monitor.connected}")
            sys.exit(1)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Error during emulation: {e}")
        sys.exit(1)
    finally:
        if mqtt_monitor:
            mqtt_monitor.stop()
        if log_monitor:
            log_monitor.stop()

        cleanup_process(daemon_proc, "daemon")
        cleanup_process(simavr_proc, "simavr")
        cleanup_process(socat_proc, "socat")

        try:
            uci_stub_dir.cleanup()  # type: ignore[name-defined]
        except Exception:
            pass

    return 0
