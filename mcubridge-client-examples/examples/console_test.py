#!/usr/bin/env python3
"""Interactive console helper for the Arduino bridge using direct LocalBridgeStub through Gateway."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated

import structlog
import typer
from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = structlog.get_logger(__name__)


async def run_test(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):

        async def console_listener() -> None:
            try:
                async with stub.SubscribeConsole.open() as stream:
                    await stream.send_message(pb.SubscribeRequest())
                    async for msg in stream:
                        payload_str = (msg.payload or b"").decode("utf-8")
                        logger.info("Received from Arduino", payload=payload_str)
            except asyncio.CancelledError:
                logger.debug("Console listener task cancelled.")
            except (OSError, RuntimeError) as e:
                logger.debug("Console listener closed", error=str(e))

        listener_task: asyncio.Task[None] = asyncio.create_task(console_listener())

        is_interactive = sys.stdin.isatty() and os.environ.get("MCUBRIDGE_NON_INTERACTIVE") != "1"
        topic_cw = Topic.build(Topic.CONSOLE, "write", prefix=topic_prefix)

        if not is_interactive:
            logger.info("Non-interactive mode. Running Echo Test (ping/pong)...")
            await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_cw, payload=b"ping", qos=1))
            await asyncio.sleep(2.0)
            logger.info("Echo Test phase completed.")
        else:
            logger.info("Enter text to send to the Arduino console. Type 'exit' to quit.")
            while True:
                try:
                    user_input = await asyncio.to_thread(input)
                    if user_input.lower() == "exit":
                        break
                    await stub.Publish(
                        pb.CloudQueuedPublish(
                            topic_name=topic_cw,
                            payload=user_input.encode("utf-8"),
                            qos=1,
                        )
                    )
                except EOFError as exc:
                    logger.info("Console input stream terminated by EOF", error=str(exc))
                    break

        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            logger.debug("Listener task cleanup complete.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix))


cli = typer.Typer(help="Interactive console helper using direct LocalBridgeStub through Gateway.", add_completion=False)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[str | None, typer.Option("--device-id", help="Target device ID")] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
) -> None:
    main(host, port, device_id, topic_prefix)


if __name__ == "__main__":
    cli()
