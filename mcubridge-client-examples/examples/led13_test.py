#!/usr/bin/env python3
"""Example: Test generic pin control using direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import asyncio
import structlog
import typer
from typing import Annotated

from mcubridge_client import pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    pin: int,
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        logger.info("--- Starting LED Pin Control Test ---")

        logger.info("Turning pin state", pin=pin, state="ON")
        await stub.DigitalWrite(pb.DigitalWrite(pin=pin, value=1))
        await asyncio.sleep(2)

        logger.info("Turning pin state", pin=pin, state="OFF")
        await stub.DigitalWrite(pb.DigitalWrite(pin=pin, value=0))
        await asyncio.sleep(2)

    logger.info("--- LED Test Complete ---")
    logger.info("Done.")


def main(
    pin: int = 13,
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(pin, host, port, device_id, topic_prefix))


cli = typer.Typer(help="Test generic pin control using direct LocalBridgeStub through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    pin: Annotated[int, typer.Argument(help="Pin number")] = 13,
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(pin, host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()
