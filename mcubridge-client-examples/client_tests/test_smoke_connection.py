#!/usr/bin/env python3
"""Minimal connectivity smoke test for the bridge client."""

from __future__ import annotations

import asyncio
import logging
import argparse
from mcubridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def run_test(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    topic: str,
) -> bool:
    """Connect, get version, then exit."""
    logging.info("Starting smoke test for %s:%s", host or "localhost", port or 1883)
    bridge = Bridge(
        host=host,
        port=port,
        user=user,
        password=password,
        base_topic=topic,
    )

    try:
        async with bridge:
            version = await bridge.system.get_version()
            logging.info("Success! MCU Version: %s", version)
            return True
    except Exception as e:
        logging.error("Smoke test FAILED: %s", e)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minimal connectivity smoke test for the bridge client."
    )
    parser.add_argument("--host", help="MQTT host")
    parser.add_argument("--port", type=int, help="MQTT port")
    parser.add_argument("--user", help="MQTT user")
    parser.add_argument("--password", help="MQTT password")
    parser.add_argument("--topic", default="bridge", help="Base topic")
    parser.add_argument(
        "--env", action="store_true", help="Dump client environment and exit"
    )

    args = parser.parse_args()

    if args.env:
        dump_client_env()
        return

    success = asyncio.run(
        run_test(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            topic=args.topic,
        )
    )
    if not success:
        import sys

        sys.exit(1)


if __name__ == "__main__":
    main()
