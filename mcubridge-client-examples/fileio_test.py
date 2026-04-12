#!/usr/bin/env python3
"""Example: Test file I/O using the async McuBridge client."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client.cli import bridge_session, configure_logging

app = typer.Typer(help="Example: Test file I/O using the async McuBridge client.")
configure_logging()


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as bridge:
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
                logging.error("FAILURE: Read content does not match written content.")

        finally:
            # --- Test File Remove ---
            logging.info("Removing %s", test_filename)
            await bridge.file_remove(test_filename)

    logging.info("Done.")


@app.command()
def main(
    host: Annotated[str | None, typer.Option(help="MQTT Broker Host")] = None,
    port: Annotated[int | None, typer.Option(help="MQTT Broker Port")] = None,
    user: Annotated[str | None, typer.Option(help="MQTT Username")] = None,
    password: Annotated[str | None, typer.Option(help="MQTT Password")] = None,
    tls_insecure: Annotated[
        bool, typer.Option(help="Disable TLS certificate verification")
    ] = False,
) -> None:
    asyncio.run(run_test(host, port, user, password, tls_insecure))


if __name__ == "__main__":
    app()
