#!/usr/bin/env python3
"""Example: Test file I/O using direct MQTT."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

import typer
from mcubridge_client import Topic
from mcubridge_client.cli import bridge_session, configure_logging

app = typer.Typer(help="Example: Test file I/O using direct MQTT.")
configure_logging()


async def mqtt_file_read(client, filename: str) -> bytes:
    read_topic = str(Topic.build(Topic.FILE, "read", filename.lstrip("/")))
    resp_topic = str(Topic.build(Topic.FILE, "read", "response", filename.lstrip("/")))
    
    await client.subscribe(resp_topic)
    await client.publish(read_topic, b"")
    
    try:
        async with asyncio.timeout(5.0):
            async for message in client.messages:
                if Topic.matches(resp_topic, str(message.topic)):
                    return bytes(message.payload) if message.payload else b""
    except asyncio.TimeoutError:
        return b""
    finally:
        await client.unsubscribe(resp_topic)
    return b""


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    tls_insecure: bool,
) -> None:

    async with bridge_session(host, port, user, password, tls_insecure) as client:
        test_filename: str = "tmp/test_file.txt"
        test_content: str = "hello from async fileio_test"

        try:
            # --- Test File Write ---
            write_topic = str(Topic.build(Topic.FILE, "write", test_filename))
            logging.info(f"Writing '{test_content}' to {write_topic}")
            await client.publish(write_topic, test_content.encode())

            await asyncio.sleep(0.5)

            # --- Test File Read ---
            logging.info(f"Reading from {test_filename}")
            content = await mqtt_file_read(client, test_filename)
            decoded = content.decode()
            logging.info("Read content: %s", decoded)

            if decoded == test_content:
                logging.info("SUCCESS: Read content matches written content.")
            else:
                logging.error("FAILURE: Read content does not match written content.")

        finally:
            # --- Test File Remove ---
            remove_topic = str(Topic.build(Topic.FILE, "remove", test_filename))
            logging.info("Removing via %s", remove_topic)
            await client.publish(remove_topic, b"")

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
