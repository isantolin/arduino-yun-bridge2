#!/usr/bin/env python3
"""Example: Test generic pin control using direct LocalBridgeStub Publish calls."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    pin: int,
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        logging.info("--- Starting LED Pin Control Test ---")
        topic_pin = Topic.build(Topic.DIGITAL, str(pin), prefix=topic_prefix)

        logging.info("Turning pin %d ON", pin)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_pin, payload=b"1", qos=1))
        await asyncio.sleep(2)

        logging.info("Turning pin %d OFF", pin)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_pin, payload=b"0", qos=1))
        await asyncio.sleep(2)

    logging.info("--- LED Test Complete ---")
    logging.info("Done.")


def main(
    pin: int = 13,
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(pin, socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test generic pin control using direct LocalBridgeStub.")
    parser.add_argument("pin", type=int, nargs="?", default=13, help="Pin number")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(
        _args.pin,
        _args.socket_path,
        _args.topic_prefix,
    )
