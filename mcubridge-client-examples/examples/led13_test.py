#!/usr/bin/env python3
"""Example: Test generic pin control using direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from mcubridge_client import pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    led_builtin_pin: int,
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        logger.info("--- Starting LED Pin Control Test ---")

        logger.info("Turning built-in LED state", pin=led_builtin_pin, state="ON")
        await stub.DigitalWrite(pb.DigitalWrite(pin=led_builtin_pin, value=1))
        await asyncio.sleep(2)

        logger.info("Turning built-in LED state", pin=led_builtin_pin, state="OFF")
        await stub.DigitalWrite(pb.DigitalWrite(pin=led_builtin_pin, value=0))
        await asyncio.sleep(2)

    logger.info("--- LED Test Complete ---")
    logger.info("Done.")


def main(
    led_builtin_pin: int,
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(led_builtin_pin, host, port, device_id, topic_prefix))


cli = typer.Typer(help="Test generic pin control using direct LocalBridgeStub through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    led_builtin_pin: Annotated[int, typer.Option("--pin", help="Pin number from the target core's LED_BUILTIN")],
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(led_builtin_pin, host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()
