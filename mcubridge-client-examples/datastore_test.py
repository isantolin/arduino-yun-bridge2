#!/usr/bin/env python3
"""Exercise datastore interactions using the bridge client."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Bridge, build_bridge_args, dump_client_env

app = typer.Typer(help="Exercise datastore interactions using the bridge client.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:

    dump_client_env(logging.getLogger(__name__))

    bridge_args = build_bridge_args(host, port, user, password, tls_insecure)
    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

    logging.info("--- Starting DataStore Bridge Client Test ---")

    # --- Test 1: Put and Get a new key-value pair ---
    logging.info("[Test 1: Put and Get a new key-value pair]")
    key1: str = "client_test/temperature"
    value1: str = "25.5"

    await bridge.put(key1, value1)
    logging.info(f"Put value '{value1}' to key '{key1}'")

    retrieved_value: str = await bridge.get(key1)
    if retrieved_value == value1:
        logging.info(
            "SUCCESS: Retrieved value '%s' matches put value '%s'.",
            retrieved_value,
            value1,
        )
    else:
        logging.error(
            "FAILURE: Retrieved value '%s' does not match put value '%s'.",
            retrieved_value,
            value1,
        )

    # --- Test 2: Get a non-existent key ---
    logging.info("\n[Test 2: Get a non-existent key]")
    key2: str = "non_existent/key"

    retrieved_value_2: str = await bridge.get(key2)
    # Expecting an empty payload for a non-existent key
    if retrieved_value_2 == "":
        logging.info(
            "SUCCESS: Empty value returned for non-existent key '%s'.",
            key2,
        )
    else:
        logging.error(
            "FAILURE: Value '%s' returned for non-existent key '%s'.",
            retrieved_value_2,
            key2,
        )

    logging.info("\n--- Test Complete ---")
    await bridge.disconnect()


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[bool, typer.Option(help="Disable TLS certificate verification")] = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
