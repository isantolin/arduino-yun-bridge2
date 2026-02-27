#!/usr/bin/env python3
"""Interactive console helper for the Arduino bridge."""

import asyncio
import logging
import ssl
import sys
from typing import Annotated

import typer
from mcubridge_client import Bridge, dump_client_env

app = typer.Typer(help="Interactive console helper for the Arduino bridge.")

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
    # Validate essential arguments if not running on OpenWrt with UCI
    if not host or not user or not password:
        from mcubridge_client.env import read_uci_general

        if not read_uci_general():
            sys.stderr.write("Error: Missing required connection parameters.\n")
            raise typer.Exit(code=1)

    dump_client_env(logging.getLogger(__name__))

    # Concise argument mapping
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

    logging.info("Enter text to send to the Arduino console. Type 'exit' to quit.")

    try:
        # Start a task to listen for console messages
        async def console_listener() -> None:
            while True:
                message = await bridge.console_read_async()
                if message is not None:
                    logging.info("Received from Arduino: %s", message)
                else:
                    await asyncio.sleep(0.1)

        listener_task: asyncio.Task[None] = asyncio.create_task(console_listener())

        while True:
            try:
                # Run blocking input in a separate thread
                user_input = await asyncio.to_thread(input)
                if user_input.lower() == "exit":
                    break
                await bridge.console_write(user_input)
            except EOFError:
                break

        # Clean up the listener task
        listener_task.cancel()
        await asyncio.gather(listener_task, return_exceptions=True)

    except asyncio.CancelledError:
        logging.info("\nExiting...")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
    finally:
        await bridge.disconnect()


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
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()
