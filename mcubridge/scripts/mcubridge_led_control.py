#!/usr/bin/env python3
"""Modernized LED control script for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
from grpclib.client import Channel
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import Topic, topic_path


def do_publish(topic: str, payload: str) -> None:
    """Publish LED state using local gRPC UNIX socket IPC."""

    async def _run():
        channel = Channel(path="/var/run/mcubridge.sock")
        stub = LocalBridgeStub(channel)
        try:
            msg = pb.CloudQueuedPublish(
                topic_name=topic,
                payload=payload.encode("utf-8"),
                qos=1,
            )
            await stub.Publish(msg)
        finally:
            channel.close()

    try:
        asyncio.run(_run())
    except Exception as e:
        sys.stderr.write(f"Error: local gRPC IPC publication failed: {e}\n")
        sys.exit(4)


def main() -> None:
    """Set the MCU pin state via CLOUD bridge."""
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Control MCU LED via CLOUD.")
    parser.add_argument("state", help="State to set (on/off)")
    parser.add_argument("pin", type=int, nargs="?", default=13, help="Pin number")
    args = parser.parse_args()

    state_norm = args.state.lower()
    if state_norm not in ("on", "off"):
        sys.stderr.write(f"Error: invalid state '{args.state}'. Use on|off.\n")
        sys.exit(2)

    config = load_runtime_config()
    topic = topic_path(config.topic_prefix, Topic.DIGITAL, args.pin)
    payload = "1" if state_norm == "on" else "0"

    do_publish(topic, payload)


if __name__ == "__main__":
    main()
