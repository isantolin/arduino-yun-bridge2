#!/usr/bin/env python3
"""Example: Send a mailbox message and read back any MCU-forwarded responses."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated

import typer
from mcubridge_client import Bridge, build_bridge_args, dump_client_env

app = typer.Typer(help="Example: Send a mailbox message and read back responses.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
    max_polls: int,
) -> None:
    if not host or not user or not password:
        from mcubridge_client.env import read_uci_general

        if not read_uci_general():
            sys.stderr.write("Error: Missing required connection parameters.\n")
            raise typer.Exit(code=1)

    dump_client_env(logger)

    bridge_args = build_bridge_args(host, port, user, password, tls_insecure)
    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

    try:
        # --- Send phase ---
        message_to_send = "hello_from_mailbox_test"
        logger.info("Sending message to mailbox: '%s'", message_to_send)
        await bridge.mailbox_write(message_to_send)
        logger.info("Message sent successfully.")

        # --- Read phase ---
        logger.info("Polling for mailbox responses (max_polls=%d)...", max_polls)
        polls = 0
        while max_polls <= 0 or polls < max_polls:
            message: bytes | None = await bridge.mailbox_read(timeout=3)
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
    finally:
        await bridge.disconnect()
        logger.info("Disconnected from MQTT broker.")

    logger.info("Done.")


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[bool, typer.Option(help="Disable TLS certificate verification")] = False,
    max_polls: Annotated[int, typer.Option(help="Max poll cycles (0 = unlimited)")] = 1,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure, max_polls))
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")


if __name__ == "__main__":
    app()
