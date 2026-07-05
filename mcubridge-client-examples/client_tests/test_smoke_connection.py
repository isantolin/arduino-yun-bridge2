#!/usr/bin/env python3
"""Minimal connectivity smoke test for the bridge client."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    dump_client_env(logging.getLogger(__name__))

    bridge_args = {}
    if socket_path:
        bridge_args["socket_path"] = socket_path
    if topic_prefix:
        bridge_args["topic_prefix"] = topic_prefix

    bridge = Bridge(**bridge_args)
    await bridge.connect()
    logging.info("Bridge connected")
    await bridge.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal connectivity smoke test for the bridge client.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    args = parser.parse_args()
    asyncio.run(run_test(args.socket_path, args.topic_prefix))


if __name__ == "__main__":
    main()
