#!/usr/bin/env python3
"""Minimal connectivity smoke test for the bridge client."""

import asyncio
import logging
from typing import Optional

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
    host: Optional[str] = typer.Option(None, help="MQTT Broker Host"),
    port: Optional[int] = typer.Option(None, help="MQTT Broker Port"),
    user: Optional[str] = typer.Option(None, help="MQTT Username"),
    password: Optional[str] = typer.Option(None, help="MQTT Password"),
) -> None:
    asyncio.run(run_test(host, port, user, password))


if __name__ == "__main__":
    app()
