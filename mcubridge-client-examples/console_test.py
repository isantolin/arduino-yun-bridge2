#!/usr/bin/env python3
"""Interactive console helper for the Arduino bridge."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as bridge:
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

    logging.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive console helper for the Arduino bridge.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)
