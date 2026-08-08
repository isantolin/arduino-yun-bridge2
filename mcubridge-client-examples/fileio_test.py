#!/usr/bin/env python3
"""Example: Test file I/O using direct LocalBridgeStub Publish calls."""

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
        test_filename: str = "/tmp/test_file.txt"
        test_content: str = "hello from async fileio_test"

        topic_fw = Topic.build(Topic.FILE, "write", prefix=topic_prefix)
        topic_fr = Topic.build(Topic.FILE, "read", prefix=topic_prefix)
        topic_frm = Topic.build(Topic.FILE, "remove", prefix=topic_prefix)

        try:
            # --- Test File Write ---
            logging.info("Writing '%s' to %s", test_content, test_filename)
            await stub.Publish(
                pb.CloudQueuedPublish(
                    topic_name=topic_fw,
                    payload=test_content.encode("utf-8"),
                    qos=1,
                )
            )

            # --- Test File Read ---
            logging.info("Reading from %s", test_filename)
            res = await stub.Publish(
                pb.CloudQueuedPublish(
                    topic_name=topic_fr,
                    payload=test_filename.encode("utf-8"),
                    qos=1,
                )
            )
            content = res.payload if res else b""
            logging.info("Read content: %s", content.decode("utf-8", errors="replace"))

        finally:
            # --- Test File Remove ---
            logging.info("Removing %s", test_filename)
            await stub.Publish(
                pb.CloudQueuedPublish(
                    topic_name=topic_frm,
                    payload=test_filename.encode("utf-8"),
                    qos=1,
                )
            )

    logging.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test file I/O using direct LocalBridgeStub.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)
