#!/usr/bin/env python3
"""Exercise datastore interactions using the bridge client."""

import asyncio
import logging
import ssl
import sys
from typing import Optional

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Exercise datastore interactions using the bridge client.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    host: Optional[str],
    port: Optional[int],
    user: Optional[str],
    password: Optional[str],
    tls_insecure: bool,
) -> None:
    # Validate essential arguments if not running on OpenWrt with UCI
    if not host or not user or not password:
        from mcubridge_client.env import read_uci_general

        if not read_uci_general():
            sys.stderr.write("Error: Missing required connection parameters.\n")
            raise typer.Exit(code=1)

    dump_client_env(logging.getLogger(__name__))

    bridge_args: dict[str, object] = {}
    if host:
        bridge_args["host"] = host
    if port:
        bridge_args["port"] = port
    if user:
        bridge_args["username"] = user
    if password:
        bridge_args["password"] = password
    if tls_insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        bridge_args["tls_context"] = ctx

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
    host: Optional[str] = typer.Option(None, help="MQTT Broker Host"),
    port: Optional[int] = typer.Option(None, help="MQTT Broker Port"),
    user: Optional[str] = typer.Option(None, help="MQTT Username"),
    password: Optional[str] = typer.Option(None, help="MQTT Password"),
    tls_insecure: bool = typer.Option(False, help="Disable TLS certificate verification"),
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
