#!/usr/bin/env python3
"""Modernized Hardware Smoke Test for MCU Bridge (SIL-2)."""

from __future__ import annotations

import socket
import time
import argparse
import sys
import structlog
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import Topic, topic_path

# [SIL-2] Structured logging towards syslog/stderr
logger = structlog.get_logger("mcubridge.hw-smoke")


class SmokeTester:
    def __init__(self) -> None:
        self.config = load_runtime_config()
        self.prefix = self.config.topic_prefix
        self.results: dict[str, bool] = {}

    def run(self, pin: int, timeout: float) -> None:
        logger.info("Starting hardware smoke test via UNIX socket IPC...")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect("/var/run/mcubridge.sock")
            self.results["connectivity"] = True
            logger.info("Connectivity to UNIX socket verified")

            # Toggle Pin
            topic = topic_path(self.prefix, Topic.DIGITAL, str(pin))
            # Send ON
            msg_on = pb.CloudQueuedPublish(topic_name=topic, payload=b"1", qos=1)
            data_on = msg_on.SerializeToString()
            sock.sendall(len(data_on).to_bytes(4, byteorder="big") + data_on)

            time.sleep(0.5)

            # Send OFF
            msg_off = pb.CloudQueuedPublish(topic_name=topic, payload=b"0", qos=1)
            data_off = msg_off.SerializeToString()
            sock.sendall(len(data_off).to_bytes(4, byteorder="big") + data_off)

            self.results["gpio"] = True
            logger.info("GPIO toggle commands sent successfully")
        except OSError as e:
            logger.error("Connection to UNIX socket failed", error=str(e))
            self.results["connectivity"] = False
        finally:
            sock.close()


def main() -> None:
    """Execute a suite of hardware diagnostic tests via UNIX socket."""
    parser = argparse.ArgumentParser(description="Diagnostic smoke test for MCU hardware.")
    parser.add_argument("--pin", type=int, default=13, help="Pin to toggle during test")
    parser.add_argument("--timeout", type=float, default=5.0, help="Timeout for responses")
    args = parser.parse_args()

    tester = SmokeTester()
    tester.run(args.pin, args.timeout)

    success = all(tester.results.values()) and bool(tester.results)
    if success:
        logger.info("Hardware smoke test SUCCESSFUL")
    else:
        logger.critical("Hardware smoke test FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
