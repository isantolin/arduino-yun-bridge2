#!/usr/bin/env python3
"""Example: Run an async shell command via direct GatewayClient Publish call."""

from __future__ import annotations

import asyncio
import shlex

import structlog
import typer
from mcubridge_client import mcu_wire_pb2
from mcubridge_client.cli import DeviceIdOption, HostOption, PortOption, bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id) as (_channel, stub):
        command_to_run = ["echo", "hello from shell"]
        cmd_str = shlex.join(command_to_run)
        logger.info("Launching command", command=cmd_str)

        res = await stub.ProcessRunAsync(mcu_wire_pb2.ProcessRunAsync(command=cmd_str))
        logger.info(
            "Shell process launched via gRPC",
            pid=res.pid,
        )

    logger.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:
    asyncio.run(run_test(host, port, device_id))


cli = typer.Typer(help="Run an async shell command via direct GatewayClient through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    host: HostOption = None,
    port: PortOption = None,
    device_id: DeviceIdOption = None,
) -> None:
    main(host, port, device_id)


if __name__ == "__main__":
    cli()
