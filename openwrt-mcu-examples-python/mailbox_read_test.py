#!/usr/bin/env python3
"""Example that listens for mailbox messages pushed from the MCU daemon."""

import asyncio
import logging
import ssl
from typing import Annotated

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Example that listens for mailbox messages pushed from the MCU daemon.")

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
) -> None:
    dump_client_env(logger)

    # Concise argument mapping
    base_args = {
        "host": host,
        "port": port,
        "username": user,
        "password": password,
    }
    bridge_args = {k: v for k, v in base_args.items() if v is not None}

    if tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        bridge_args["tls_context"] = ctx

    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

    try:
        logger.info("Waiting for mailbox messages. Press Ctrl+C to stop.")
        while True:
            message: bytes | None = await bridge.mailbox_read(timeout=10)
            if message is None:
                logger.info("No mailbox message within timeout; still listening...")
                continue

            preview = message.decode("utf-8", errors="ignore")
            logger.info(
                "Received mailbox message (%d bytes): %s",
                len(message),
                preview,
            )
    finally:
        await bridge.disconnect()
        logger.info("Disconnected from MQTT broker.")


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure))
    except KeyboardInterrupt:
        logger.info("Exiting due to KeyboardInterrupt.")


if __name__ == "__main__":
    app()
