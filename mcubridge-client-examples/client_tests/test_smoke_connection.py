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
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
) -> None:
    dump_client_env(logging.getLogger(__name__))

    bridge_args = {}
    if host:
        bridge_args["host"] = host
    if port:
        bridge_args["port"] = port
    if user:
        bridge_args["username"] = user
    if password:
        bridge_args["password"] = password

    bridge = Bridge(**bridge_args)
    await bridge.connect()
    logging.info("Bridge connected")
    await bridge.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minimal connectivity smoke test for the bridge client."
    )
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    args = parser.parse_args()
    asyncio.run(run_test(args.host, args.port, args.user, args.password))


if __name__ == "__main__":
    main()
