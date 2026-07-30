#!/usr/bin/env python3
"""Modernized LED control script for MCU Bridge (SIL-2)."""

# pyright: reportUnknownMemberType=false
from __future__ import annotations
from typing import Annotated

import asyncio
import sys
from grpclib.client import Channel
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import Topic, topic_path


import typer

app = typer.Typer(help="Control MCU LED via CLOUD.", add_completion=False)


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
    except (OSError, RuntimeError, ValueError) as e:
        sys.stderr.write(f"Error: local gRPC IPC publication failed: {e}\n")
        sys.exit(4)


@app.command()
def main(
    state: Annotated[str, typer.Argument(help="State to set (on/off)")],
    pin: Annotated[int, typer.Argument(help="Pin number")] = 13,
) -> None:
    """Set the MCU pin state via CLOUD bridge."""
    state_norm = state.lower()
    if state_norm not in ("on", "off"):
        sys.stderr.write(f"Error: invalid state '{state}'. Use on|off.\n")
        sys.exit(2)

    config = load_runtime_config()
    topic = topic_path(config.topic_prefix, Topic.DIGITAL, pin)
    payload = "1" if state_norm == "on" else "0"

    do_publish(topic, payload)


if __name__ == "__main__":
    app()
