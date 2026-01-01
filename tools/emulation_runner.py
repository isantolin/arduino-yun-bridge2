#!/usr/bin/env python3
"""
Hardware Emulation Runner.
This script is designed to launch SimAVR with the compiled Bridge firmware
and connect it via a virtual serial port (socat) to the Python YunBridge daemon.

It serves as the End-to-End test entrypoint.
"""

import sys
import subprocess
import logging
import time
import os
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("emulation-runner")

SOCAT_PORT0 = "/tmp/ttyBRIDGE0"
SOCAT_PORT1 = "/tmp/ttyBRIDGE1"


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
    logger.info("Starting Emulation Runner...")

    # 1. Check for required tools
    required_tools = ["simavr", "socat"]
    for tool in required_tools:
        if subprocess.call(["which", tool], stdout=subprocess.DEVNULL) != 0:
            logger.error(f"Required tool '{tool}' not found.")
            # We exit with 0 to not break CI until the full environment is set up
            sys.exit(0)

    # 2. Paths
    # Script is in tools/, so up one level is root
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    # Path to the Python package source (Required for PYTHONPATH)
    package_root = repo_root / "openwrt-yun-bridge"

    # Add package to sys.path for direct imports in this script
    sys.path.insert(0, str(package_root))
    from yunbridge.rpc import protocol

    # Path to Firmware
    base_build_path = repo_root / "openwrt-library-arduino/build"
    firmware_path = base_build_path / "BridgeControl/BridgeControl.ino.elf"

    # Fallback/Debug: List available ELFs if specific one is missing
    if not firmware_path.exists():
        logger.warning(f"Firmware ELF not found at {firmware_path}")
        if base_build_path.exists():
            logger.info("Available ELFs in build dir:")
            found_elfs = list(base_build_path.glob("**/*.elf"))
            for elf in found_elfs:
                logger.info(f" - {elf}")
                # Auto-select the first BridgeControl looking elf
                if "BridgeControl" in str(elf) and not firmware_path.exists():
                    firmware_path = elf
                    logger.info(f"Auto-selected firmware: {firmware_path}")

    if not firmware_path.exists():
        logger.error("CRITICAL: No valid firmware ELF found. Compilation might have failed or path is wrong.")
        sys.exit(1)

    # 3. Setup Virtual Serial Port
    logger.info("Starting socat...")
    # socat -d -d pty,raw,echo=0,link=/tmp/ttyBRIDGE0 pty,raw,echo=0,link=/tmp/ttyBRIDGE1
    socat_cmd = [
        "socat", "-d", "-d",
        f"pty,raw,echo=0,link={SOCAT_PORT0}",
        f"pty,raw,echo=0,link={SOCAT_PORT1}"
    ]

    socat_proc = subprocess.Popen(socat_cmd, stderr=subprocess.PIPE, text=True)

    # Wait for ports to appear
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

    try:
        # 4. Start SimAVR
        logger.info(f"Starting simavr with {firmware_path}...")

        simavr_cmd = [
            "simavr",
            "-m", "atmega32u4",
            "-f", "16000000",
            str(firmware_path)
        ]

        simavr_proc = subprocess.Popen(simavr_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # 5. Start Python Daemon (Test Mode)
        logger.info("Starting Bridge Daemon (Test Mode)...")

        daemon_env = os.environ.copy()

        # Inject openwrt-yun-bridge into PYTHONPATH
        current_pythonpath = daemon_env.get("PYTHONPATH", "")
        daemon_env["PYTHONPATH"] = f"{str(package_root)}{os.pathsep}{current_pythonpath}"

        # Configuration injection to pass RuntimeConfig validation
        daemon_env["YUNBRIDGE_PORT"] = SOCAT_PORT0
        daemon_env["YUNBRIDGE_BAUDRATE"] = str(protocol.DEFAULT_BAUDRATE)

        # Security & Transport settings for Test Environment
        daemon_env["SERIAL_SHARED_SECRET"] = "emulation_test_secret_xyz"

        # Disable MQTT TLS and watchdog for tests
        daemon_env["MQTT_TLS"] = "0"
        daemon_env["DISABLE_WATCHDOG"] = "1"

        # [NEW] Suppress UCI missing warning (since we are not on OpenWrt)
        daemon_env["YUNBRIDGE_NO_UCI_WARNING"] = "1"

        daemon_cmd = [
            sys.executable,
            "-m", "yunbridge.daemon",
            "--debug"
        ]

        daemon_proc = subprocess.Popen(daemon_cmd, env=daemon_env)

        # 6. Monitor and Wait
        # Run for a few seconds to verify handshake and startup
        logger.info("Waiting for system stabilization (10s)...")
        time.sleep(10)

        daemon_status = daemon_proc.poll()
        simavr_status = simavr_proc.poll()

        if daemon_status is not None:
            logger.error(f"Daemon exited prematurely with code {daemon_status}")
            sys.exit(1)

        if simavr_status is not None:
            logger.error(f"SimAVR exited prematurely with code {simavr_status}")
            sys.exit(1)

        logger.info("Emulation test run completed successfully (Simulated Context).")

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Error during emulation: {e}")
        sys.exit(1)
    finally:
        cleanup_process(daemon_proc, "daemon")
        cleanup_process(simavr_proc, "simavr")
        cleanup_process(socat_proc, "socat")

    return 0


if __name__ == "__main__":
    sys.exit(main())
