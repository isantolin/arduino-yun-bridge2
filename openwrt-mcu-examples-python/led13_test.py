#!/usr/bin/env python3
"""Example: Test generic pin control using the async McuBridge client."""

import asyncio
import logging
import ssl
from typing import Optional

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Test generic pin control using the async McuBridge client.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    pin: int,
    host: Optional[str],
    port: Optional[int],
    user: Optional[str],
    password: Optional[str],
    tls_insecure: bool,
) -> None:
    dump_client_env(logging.getLogger(__name__))

    # Build arguments dict, only including provided values to allow Bridge defaults (env vars) to work
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

    try:
        logging.info(f"Turning pin {pin} ON")
        await bridge.digital_write(pin, 1)
        await asyncio.sleep(2)

        logging.info(f"Turning pin {pin} OFF")
        await bridge.digital_write(pin, 0)
        await asyncio.sleep(2)

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()

    logging.info("Done.")


@app.command()
def main(
    pin: int = typer.Argument(13, help="Pin number"),
    host: Optional[str] = typer.Option(None, help="MQTT Broker Host"),
    port: Optional[int] = typer.Option(None, help="MQTT Broker Port"),
    user: Optional[str] = typer.Option(None, help="MQTT Username"),
    password: Optional[str] = typer.Option(None, help="MQTT Password"),
    tls_insecure: bool = typer.Option(False, help="Disable TLS certificate verification"),
) -> None:
    asyncio.run(run_test(pin, host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
