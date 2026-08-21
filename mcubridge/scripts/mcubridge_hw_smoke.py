#!/usr/bin/env python3
"""Modernized Hardware Smoke Test for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
from typing import Annotated

import structlog
import typer
from grpclib.client import Channel

from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub

# [SIL-2] Structured logging towards syslog/stderr
logger = structlog.get_logger("mcubridge.hw-smoke")


class SmokeTester:
    def __init__(self) -> None:
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

                # Toggle Pin ON
                async with asyncio.timeout(timeout):
                    await stub.DigitalWrite(pb.DigitalWrite(pin=pin, value=1))

                await asyncio.sleep(0.5)

                # Toggle Pin OFF
                async with asyncio.timeout(timeout):
                    await stub.DigitalWrite(pb.DigitalWrite(pin=pin, value=0))

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
