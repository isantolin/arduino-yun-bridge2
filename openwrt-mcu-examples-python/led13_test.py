#!/usr/bin/env python3
"""Example: Test generic pin control using the async McuBridge client."""

import asyncio
import logging
import ssl
from typing import Optional, Annotated

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

    # Concise argument mapping using a comprehension to filter provided CLI options
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
    pin: Annotated[int, typer.Argument(help="Pin number")] = 13,
    host: Annotated[Optional[str], typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[Optional[int], typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[Optional[str], typer.Option(help="MQTT Username")] = None,
    password: Annotated[Optional[str], typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    asyncio.run(run_test(pin, host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
