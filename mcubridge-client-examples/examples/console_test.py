#!/usr/bin/env python3
"""Console write helper for the Arduino bridge using direct GatewayClient through Gateway.

Linux -> MCU console bytes travel through ``ConsoleService/Write``. The MCU -> Linux
console output is published by the daemon as telemetry on ``evt/ConsoleService/Write``;
a northbound Gateway -> client stream for that topic does not exist yet (ADR-0003), so
this helper only writes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated

import structlog
import typer
from mcubridge_client import mcu_wire_pb2
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id) as (_channel, stub):
        is_interactive = sys.stdin.isatty() and os.environ.get("MCUBRIDGE_NON_INTERACTIVE") != "1"

        if not is_interactive:
            logger.info("Non-interactive mode. Writing 'ping' to the MCU console...")
            await stub.ConsoleWrite(mcu_wire_pb2.ConsoleWrite(data=b"ping"))
            await asyncio.sleep(2.0)
            logger.info("Console write phase completed.")
            return

        logger.info("Enter text to send to the Arduino console. Type 'exit' to quit.")
        while True:
            try:
                user_input = await asyncio.to_thread(input)
            except EOFError as exc:
                logger.info("Console input stream terminated by EOF", error=str(exc))
                break
            if user_input.lower() == "exit":
                break
            await stub.ConsoleWrite(mcu_wire_pb2.ConsoleWrite(data=user_input.encode("utf-8")))


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
) -> None:
    asyncio.run(run_test(host, port, device_id))


cli = typer.Typer(help="Console write helper using direct GatewayClient through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[str | None, typer.Option("--device-id", help="Target device ID")] = None,
) -> None:
    main(host, port, device_id)


if __name__ == "__main__":
    cli()
