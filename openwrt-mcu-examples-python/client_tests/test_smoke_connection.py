#!/usr/bin/env python3
"""Minimal connectivity smoke test for the bridge client."""

import argparse
import asyncio
import logging

from mcubridge_client import Bridge, dump_client_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal connectivity smoke test.")
    parser.add_argument("--host", default=None, help="MQTT Broker Host")
    parser.add_argument("--port", type=int, default=None, help="MQTT Broker Port")
    parser.add_argument("--user", default=None, help="MQTT Username")
    parser.add_argument("--password", default=None, help="MQTT Password")
    args = parser.parse_args()

    dump_client_env(logging.getLogger(__name__))

    bridge_args = {}
    if args.host:
        bridge_args["host"] = args.host
    if args.port:
        bridge_args["port"] = args.port
    if args.user:
        bridge_args["username"] = args.user
    if args.password:
        bridge_args["password"] = args.password

    bridge = Bridge(**bridge_args)
    await bridge.connect()
    logging.info("Bridge connected")
    await bridge.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
