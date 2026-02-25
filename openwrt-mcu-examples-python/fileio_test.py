#!/usr/bin/env python3
"""Example: Test file I/O using the async McuBridge client."""

import asyncio
import logging
import ssl
import sys
from typing import Optional, Annotated

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Example: Test file I/O using the async McuBridge client.")

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

    test_filename: str = "/tmp/test_file.txt"
    test_content: str = "hello from async fileio_test"

    try:
        # --- Test File Write ---
        logging.info(f"Writing '{test_content}' to {test_filename}")
        await bridge.file_write(test_filename, test_content)

        # --- Test File Read ---
        logging.info(f"Reading from {test_filename}")
        content: bytes = await bridge.file_read(test_filename)
        decoded = content.decode()
        logging.info("Read content: %s", decoded)

        if decoded == test_content:
            logging.info("SUCCESS: Read content matches written content.")
        else:
            logging.error("FAILURE: Read content does not match written " "content.")

    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        # --- Test File Remove ---
        logging.info("Removing %s", test_filename)
        await bridge.file_remove(test_filename)
        await bridge.disconnect()

    logging.info("Done.")


@app.command()
def main(
    host: Annotated[Optional[str], typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[Optional[int], typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[Optional[str], typer.Option(help="MQTT Username")] = None,
    password: Annotated[Optional[str], typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
