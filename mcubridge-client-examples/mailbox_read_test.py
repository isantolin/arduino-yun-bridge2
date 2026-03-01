#!/usr/bin/env python3
"""Example that listens for mailbox messages pushed from the MCU daemon."""

import asyncio
import logging
from typing import Optional, Annotated

import typer
from mcubridge_client import Bridge, build_bridge_args, dump_client_env

app = typer.Typer(help="Example that listens for mailbox messages pushed from the MCU daemon.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def run_test(
    host: Optional[str],
    port: Optional[int],
    user: Optional[str],
    password: Optional[str],
    tls_insecure: bool,
    max_polls: int,
) -> None:
    dump_client_env(logger)

    bridge_args = build_bridge_args(host, port, user, password, tls_insecure)
    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

    try:
        logger.info("Waiting for mailbox messages. Press Ctrl+C to stop.")
        polls = 0
        while max_polls <= 0 or polls < max_polls:
            message: bytes | None = await bridge.mailbox_read(timeout=10)
            polls += 1
            if message is None:
                logger.info("No mailbox message within timeout; still listening...")
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


@app.command()
def main(
    host: Annotated[Optional[str], typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[Optional[int], typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[Optional[str], typer.Option(help="MQTT Username")] = None,
    password: Annotated[Optional[str], typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[bool, typer.Option(help="Disable TLS certificate verification")] = False,
    max_polls: Annotated[int, typer.Option(help="Max poll cycles (0 = unlimited)")] = 0,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure, max_polls))
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")


if __name__ == "__main__":
    app()
