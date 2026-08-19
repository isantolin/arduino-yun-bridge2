#!/usr/bin/env python3
"""Example: Test generic pin control using direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import asyncio
import structlog
import typer
from typing import Annotated

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    pin: int,
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        logger.info("--- Starting LED Pin Control Test ---")
        topic_pin = Topic.build(Topic.DIGITAL, str(pin), prefix=topic_prefix)

        logger.info("Turning pin %d ON", pin)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_pin, payload=b"1", qos=1))
        await asyncio.sleep(2)

        logger.info("Turning pin %d OFF", pin)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_pin, payload=b"0", qos=1))
        await asyncio.sleep(2)

    logger.info("--- LED Test Complete ---")
    logger.info("Done.")


def main(
    pin: int = 13,
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(pin, socket_path, topic_prefix))


cli = typer.Typer(help="Test generic pin control using direct LocalBridgeStub.", add_completion=False)


@cli.command()
def cli_main(
    pin: Annotated[int, typer.Argument(help="Pin number")] = 13,
    socket_path: Annotated[str | None, typer.Option("--socket-path", help="UNIX Domain Socket Path")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(pin, socket_path, topic_prefix)


if __name__ == "__main__":
    cli()
