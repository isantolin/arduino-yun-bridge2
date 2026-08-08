#!/usr/bin/env python3
"""Exercise datastore interactions using direct LocalBridgeStub and Channel."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        logging.info("--- Starting DataStore Bridge Client Test ---")

        # --- Test 1: Put a new key-value pair ---
        logging.info("[Test 1: Put a new key-value pair]")
        key1: str = "client_test/temperature"
        value1: str = "25.5"

        topic_ds = Topic.build(Topic.DATASTORE, "put", key1, prefix=topic_prefix)
        await stub.Publish(pb.CloudQueuedPublish(topic_name=topic_ds, payload=value1.encode("utf-8"), qos=1))
        logging.info(f"Put value '{value1}' to key '{key1}'")

    logging.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exercise datastore interactions using direct LocalBridgeStub.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)
