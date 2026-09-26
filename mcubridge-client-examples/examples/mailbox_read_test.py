#!/usr/bin/env python3
"""Example: Send a mailbox message and read back any MCU-forwarded responses using direct LocalBridgeStub."""

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
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
    max_polls: int = 1,
) -> None:

    async with bridge_session(host=host, port=port, device_id=device_id, topic_prefix=topic_prefix) as (_channel, stub):
        logger.info("--- Starting Mailbox Read Test ---")

        # --- Send phase ---
        message_to_send = "hello_from_mailbox_test"
        logger.info("Sending message to mailbox", message=message_to_send)
        await stub.MailboxPush(pb.MailboxPush(data=message_to_send.encode("utf-8")))
        logger.info("Message sent successfully.")

        # --- Read phase ---
        logger.info("Polling for mailbox responses", max_polls=max_polls)
        polls = 0
        while max_polls <= 0 or polls < max_polls:
            res = await stub.MailboxRead(pb.SubscribeRequest())
            polls += 1
            message: bytes | None = res.content if (res and res.content) else None
            if not message:
                logger.info("No mailbox message within timeout", poll=polls)
                continue

            try:
                preview = message.decode("utf-8")
            except UnicodeDecodeError:
                preview = f"<hex:{message.hex()}>"
            logger.info(
                "Received mailbox message",
                bytes_length=len(message),
                preview=preview,
            )
        if max_polls > 0:
            logger.info("Reached max polls, exiting", max_polls=max_polls)

    logger.info("Done.")


def main(
    host: str | None = None,
    port: int | None = None,
    device_id: str | None = None,
    topic_prefix: str = "br",
    max_polls: int = 1,
) -> None:
    asyncio.run(run_test(host, port, device_id, topic_prefix, max_polls))


cli = typer.Typer(
    help="Send a mailbox message and read back responses using direct LocalBridgeStub through Gateway.",
    add_completion=False,
)


@cli.command()
def cli_main(
    host: Annotated[str | None, typer.Option("--host", help="Cloud Gateway host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Cloud Gateway port")] = None,
    device_id: Annotated[
        str | None, typer.Option("--device-id", help="Explicit target device ID", envvar="MCUBRIDGE_DEVICE_ID")
    ] = None,
    topic_prefix: Annotated[str, typer.Option("--topic-prefix", help="Topic prefix")] = "br",
    max_polls: Annotated[int, typer.Option("--max-polls", help="Max read attempts (0=infinite)")] = 1,
) -> None:
    main(host, port, device_id, topic_prefix, max_polls)


if __name__ == "__main__":
    cli()
