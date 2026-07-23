#!/usr/bin/env python3
"""Example: Send a mailbox message and read back any MCU-forwarded responses using direct LocalBridgeStub."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()
logger = logging.getLogger(__name__)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
    max_polls: int,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        logger.info("--- Starting Mailbox Read Test ---")

        topic_mw = Topic.build(Topic.MAILBOX, "write", prefix=topic_prefix)
        topic_mr = Topic.build(Topic.MAILBOX, "read", prefix=topic_prefix)

        # --- Send phase ---
        message_to_send = "hello_from_mailbox_test"
        logger.info("Sending message to mailbox: '%s'", message_to_send)
        await stub.Publish(
            pb.CloudQueuedPublish(
                topic_name=topic_mw,
                payload=message_to_send.encode("utf-8"),
                qos=1,
            )
        )
        logger.info("Message sent successfully.")

        # --- Read phase ---
        logger.info("Polling for mailbox responses (max_polls=%d)...", max_polls)
        polls = 0
        while max_polls <= 0 or polls < max_polls:
            res = await stub.Publish(
                pb.CloudQueuedPublish(
                    topic_name=topic_mr,
                    payload=b"",
                    qos=1,
                )
            )
            polls += 1
            message: bytes | None = res.payload if (res and res.payload) else None
            if message is None:
                logger.info("No mailbox message within timeout; poll %d done.", polls)
                continue

            try:
                preview = message.decode("utf-8")
            except UnicodeDecodeError:
                preview = f"<hex:{message.hex()}>"
            logger.info(
                "Received mailbox message (%d bytes): %s",
                len(message),
                preview,
            )
        if max_polls > 0:
            logger.info("Reached max polls (%d), exiting.", max_polls)

    logger.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
    max_polls: int = 1,
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix, max_polls))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send a mailbox message and read back responses using direct LocalBridgeStub."
    )
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    parser.add_argument(
        "--max-polls",
        type=int,
        default=1,
        help="Max read attempts (0=infinite)",
    )
    _args = parser.parse_args()
    main(
        _args.socket_path,
        _args.topic_prefix,
        _args.max_polls,
    )
