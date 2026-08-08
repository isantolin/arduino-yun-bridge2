#!/usr/bin/env python3
"""Modernized Hardware Smoke Test for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
from grpclib.client import Channel
import structlog
import typer
from typing import Annotated
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
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
        logger.info("Starting hardware smoke test via local gRPC IPC...")

        async def _run():
            channel = None
            try:
                channel = Channel(path="/var/run/mcubridge.sock")
                stub = LocalBridgeStub(channel)
                self.results["connectivity"] = True
                logger.info("Connectivity to local gRPC socket verified")

                # Toggle Pin
                topic = topic_path(self.prefix, Topic.DIGITAL, str(pin))
                # Send ON — bound only the RPC response wait to `timeout`,
                # not the fixed inter-toggle delay below. [SIL-2]
                msg_on = pb.CloudQueuedPublish(topic_name=topic, payload=b"1", qos=1)
                async with asyncio.timeout(timeout):
                    await stub.Publish(msg_on)

                await asyncio.sleep(0.5)

                # Send OFF
                msg_off = pb.CloudQueuedPublish(topic_name=topic, payload=b"0", qos=1)
                async with asyncio.timeout(timeout):
                    await stub.Publish(msg_off)

                self.results["gpio"] = True
                logger.info("GPIO toggle commands sent successfully")
            except (OSError, RuntimeError, ValueError, TimeoutError) as e:
                logger.error("Connection or call to local gRPC socket failed", error=str(e))
                self.results["connectivity"] = False
            finally:
                if channel is not None:
                    channel.close()

        asyncio.run(_run())


cli = typer.Typer(help="Diagnostic smoke test for MCU hardware.", add_completion=False)


@cli.command()
def main(
    pin: Annotated[int, typer.Option("--pin", help="Pin to toggle during test")] = 13,
    timeout: Annotated[float, typer.Option("--timeout", help="Timeout for responses")] = 5.0,
) -> None:
    """Execute a suite of hardware diagnostic tests via UNIX socket."""
    tester = SmokeTester()
    tester.run(pin, timeout)

    success = all(tester.results.values()) and bool(tester.results)
    if success:
        logger.info("Hardware smoke test SUCCESSFUL")
    else:
        logger.critical("Hardware smoke test FAILED")
        sys.exit(1)


if __name__ == "__main__":
    cli()
