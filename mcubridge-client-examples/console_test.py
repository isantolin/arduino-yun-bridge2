#!/usr/bin/env python3
"""Interactive console helper for the Arduino bridge using direct LocalBridgeStub."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):

        async def console_listener() -> None:
            try:
                async with stub.SubscribeConsole.open() as stream:
                    await stream.send_message(pb.SubscribeRequest())
                    async for msg in stream:
                        payload_str = (msg.payload or b"").decode("utf-8", errors="replace")
                        logging.info("Received from Arduino: %s", payload_str)
            except asyncio.CancelledError:
                pass
            except (OSError, RuntimeError) as e:
                logging.debug("Console listener closed: %s", e)

        listener_task: asyncio.Task[None] = asyncio.create_task(console_listener())

        is_interactive = sys.stdin.isatty() and os.environ.get("MCUBRIDGE_NON_INTERACTIVE") != "1"
        topic_cw = Topic.build(Topic.CONSOLE, "write", prefix=topic_prefix)

        if not is_interactive:
            logging.info("Non-interactive mode. Running Echo Test (ping/pong)...")
            await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_cw, payload=b"ping", qos=1))
            await asyncio.sleep(2.0)
            logging.info("Echo Test phase completed.")
        else:
            logging.info("Enter text to send to the Arduino console. Type 'exit' to quit.")
            while True:
                try:
                    user_input = await asyncio.to_thread(input)
                    if user_input.lower() == "exit":
                        break
                    await stub.Publish(
                        pb.CloudQueuedPublish(
                            topic_name=topic_cw,
                            payload=user_input.encode("utf-8"),
                            qos=1,
                        )
                    )
                except EOFError:
                    break

        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive console helper using direct LocalBridgeStub.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)
