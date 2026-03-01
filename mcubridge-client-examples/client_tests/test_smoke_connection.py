#!/usr/bin/env python3
"""Minimal connectivity smoke test for the bridge client."""

import asyncio
import logging
from typing import Optional, Annotated

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Minimal connectivity smoke test for the bridge client.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    host: Optional[str],
    port: Optional[int],
    user: Optional[str],
    password: Optional[str],
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
    host: Annotated[Optional[str], typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[Optional[int], typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[Optional[str], typer.Option(help="MQTT Username")] = None,
    password: Annotated[Optional[str], typer.Option(help="MQTT Password")] = None,
) -> None:
    asyncio.run(run_test(host, port, user, password))


if __name__ == "__main__":
    app()
