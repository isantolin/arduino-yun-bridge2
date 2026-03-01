#!/usr/bin/env python3
"""Example: Test mailbox feature using the async McuBridge client."""

import asyncio
import logging
import sys
from typing import Optional, Annotated

import typer
from mcubridge_client import Bridge, build_bridge_args, dump_client_env

app = typer.Typer(help="Example: Test mailbox feature using the async McuBridge client.")

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

    bridge_args = build_bridge_args(host, port, user, password, tls_insecure)
    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]

    try:
        await bridge.connect()

        message_to_send: str = "hello_from_async_client"
        logging.info("Sending message to mailbox: '%s'", message_to_send)

        # Send the message
        await bridge.mailbox_write(message_to_send)
        logging.info("Message sent successfully.")

        # Keep connection open briefly to ensure transmission
        await asyncio.sleep(2)

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


@app.command()
def main(
    host: Annotated[Optional[str], typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[Optional[int], typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[Optional[str], typer.Option(help="MQTT Username")] = None,
    password: Annotated[Optional[str], typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[bool, typer.Option(help="Disable TLS certificate verification")] = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
