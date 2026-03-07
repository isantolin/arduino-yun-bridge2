#!/usr/bin/env python3
"""Interactive console helper for the Arduino bridge."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated

import typer
from mcubridge_client import Bridge, build_bridge_args, dump_client_env

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

    dump_client_env(logging.getLogger(__name__))

    bridge_args = build_bridge_args(host, port, user, password, tls_insecure)
    bridge = Bridge(**bridge_args)  # type: ignore[arg-type]
    await bridge.connect()

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

        # [CI] Automatic Echo Test if not in a TTY or forced via env
        is_interactive = sys.stdin.isatty() and os.environ.get("MCUBRIDGE_NON_INTERACTIVE") != "1"

        if not is_interactive:
            logging.info("Non-interactive mode. Running Echo Test (ping/pong)...")
            await bridge.console_write("ping")

            # Wait up to 5 seconds for a response
            start = asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - start < 5.0:
                # The listener task will log the pong if it arrives
                await asyncio.sleep(0.5)
            logging.info("Echo Test phase completed.")
        else:
            logging.info("Enter text to send to the Arduino console. Type 'exit' to quit.")
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
    tls_insecure: Annotated[bool, typer.Option(help="Disable TLS certificate verification")] = False,
) -> None:
    try:
        asyncio.run(run_test(host, port, user, password, tls_insecure))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()
