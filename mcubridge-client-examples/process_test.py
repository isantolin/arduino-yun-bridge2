#!/usr/bin/env python3
"""Example: Run an async shell command via direct LocalBridgeStub Publish call."""

from __future__ import annotations

import argparse
import asyncio
import logging
import shlex

from mcubridge_client import Topic, pb
from mcubridge_client.cli import bridge_session, configure_logging

configure_logging()


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:

    async with bridge_session(socket_path, topic_prefix) as (_channel, stub):
        command_to_run = ["echo", "hello from shell"]
        cmd_str = shlex.join(command_to_run)
        logging.info("Launching command: %s", cmd_str)

        topic_shell = Topic.build(Topic.SHELL, "run_async", prefix=topic_prefix)
        payload = pb.ProcessRunAsync(command=cmd_str).SerializeToString()

        msg = pb.CloudQueuedPublish(topic_name=topic_shell, payload=payload, qos=1)
        res = await stub.Publish(msg)
        logging.info(
            "Shell run_async published to %s (response topic: %s)",
            topic_shell,
            res.topic_name if res else "",
        )

    logging.info("Done.")


def main(
    socket_path: str | None = None,
    topic_prefix: str = "br",
) -> None:
    asyncio.run(run_test(socket_path, topic_prefix))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an async shell command via direct LocalBridgeStub Publish.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    _args = parser.parse_args()
    main(_args.socket_path, _args.topic_prefix)
