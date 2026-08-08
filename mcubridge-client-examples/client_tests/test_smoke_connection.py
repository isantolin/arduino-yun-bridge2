#!/usr/bin/env python3
"""Minimal connectivity smoke test for LocalBridgeStub and Channel using bridge_session."""

from __future__ import annotations

import argparse
import asyncio
import logging

from mcubridge_client import dump_client_env
from mcubridge_client.cli import bridge_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    socket_path: str | None,
    topic_prefix: str,
) -> None:
    dump_client_env(logging.getLogger(__name__))

    async with bridge_session(socket_path, topic_prefix) as (_channel, _stub):
        logging.info("Bridge channel initialized via bridge_session")


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal connectivity smoke test for LocalBridgeStub and Channel.")
    parser.add_argument("--socket-path", default=None, help="UNIX Domain Socket Path")
    parser.add_argument("--topic-prefix", default="br", help="Topic prefix")
    args = parser.parse_args()
    asyncio.run(run_test(args.socket_path, args.topic_prefix))


if __name__ == "__main__":
    main()
