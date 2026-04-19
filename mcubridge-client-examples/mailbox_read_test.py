#!/usr/bin/env python3
"""Example: Send a mailbox message and read back responses using direct MQTT."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Topic
from mcubridge_client.cli import bridge_session, configure_logging

app = typer.Typer(help="Example: Send a mailbox message and read back responses.")
configure_logging()
logger = logging.getLogger(__name__)


async def mqtt_mailbox_read(client, timeout: float = 5.0) -> bytes | None:
    read_topic = str(Topic.build(Topic.MAILBOX, "read"))
    resp_topic = str(Topic.build(Topic.MAILBOX, "incoming"))

    await client.subscribe(resp_topic)
    await client.publish(read_topic, b"")

    try:
        async with asyncio.timeout(timeout):
            async for message in client.messages:
                if Topic.matches(resp_topic, str(message.topic)):
                    return bytes(message.payload) if message.payload else b""
    except asyncio.TimeoutError:
        return None
    finally:
        await client.unsubscribe(resp_topic)
    return None


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
    max_polls: int,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as client:
        logger.info("--- Starting Mailbox Read Direct MQTT Test ---")

        # --- Send phase ---
        message_to_send = "hello_from_mailbox_test"
        write_topic = str(Topic.build(Topic.MAILBOX, "write"))
        logger.info("Sending message to mailbox: '%s' via %s", message_to_send, write_topic)
        await client.publish(write_topic, message_to_send.encode())
        logger.info("Message sent successfully.")

        # --- Read phase ---
        logger.info("Polling for mailbox responses (max_polls=%d)...", max_polls)
        polls = 0
        while max_polls <= 0 or polls < max_polls:
            message = await mqtt_mailbox_read(client, timeout=3)
            polls += 1
            if message is None:
                logger.info("No mailbox message within timeout; poll %d done.", polls)
                continue

            preview = message.decode("utf-8", errors="ignore")
            logger.info(
                "Received mailbox message (%d bytes): %s",
                len(message),
                preview,
            )
        if max_polls > 0:
            logger.info("Reached max polls (%d), exiting.", max_polls)

    logger.info("Done.")


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
    max_polls: Annotated[int, typer.Option(help="Max poll cycles (0 = unlimited)")] = 1,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure, max_polls))
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")


if __name__ == "__main__":
    app()
