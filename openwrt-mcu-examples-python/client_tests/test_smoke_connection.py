#!/usr/bin/env python3
"""Minimal connectivity smoke test for the bridge client."""

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Minimal connectivity smoke test for the bridge client.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
) -> None:
    dump_client_env(logging.getLogger(__name__))

    bridge_args = {}
    if host:
        bridge_args["host"] = host
    if port:
        bridge_args["port"] = port
    if user:
        bridge_args["username"] = user
    if password:
        bridge_args["password"] = password

    bridge = Bridge(**bridge_args)
    await bridge.connect()
    logging.info("Bridge connected")
    await bridge.disconnect()


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
) -> None:
    asyncio.run(run_test(host, port, user, password))


if __name__ == "__main__":
    app()
