#!/usr/bin/env python3
"""Example: Test generic pin control using the async McuBridge client."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Topic, build_bridge_args, get_client
from mcubridge_client.cli import configure_logging

app = typer.Typer(help="Test generic pin control using the async McuBridge client.")
configure_logging()


async def run_test(
    pin: int,
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:

    async with get_client(**build_bridge_args(host, port, user, password, tls_insecure)) as client:
        logging.info("--- Starting LED Pin Control Test ---")

        logging.info(f"Turning pin {pin} ON")
        await client.publish(Topic.build(Topic.DIGITAL, pin), b"1")
        await asyncio.sleep(2)

        logging.info(f"Turning pin {pin} OFF")
        await client.publish(Topic.build(Topic.DIGITAL, pin), b"0")
        await asyncio.sleep(2)

    logging.info("--- LED Test Complete ---")

    logging.info("Done.")


@app.command()
def main(
    pin: Annotated[int, typer.Argument(help="Pin number")] = 13,
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    asyncio.run(run_test(pin, host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
