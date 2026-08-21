#!/usr/bin/env python3
"""Modernized LED control script for MCU Bridge (SIL-2)."""

from __future__ import annotations
from typing import Annotated

import asyncio
import sys
from grpclib.client import Channel
from mcubridge.protocol.mcubridge_grpc import LocalBridgeStub
from mcubridge.protocol import mcubridge_pb2 as pb

import typer

app = typer.Typer(help="Control MCU LED via CLOUD.", add_completion=False)


def set_led_state(pin: int, value: int) -> None:
    """Set LED state using local gRPC UNIX socket IPC."""

    async def _run():
        async with Channel(path="/var/run/mcubridge.sock") as channel:
            stub = LocalBridgeStub(channel)
            await stub.DigitalWrite(pb.DigitalWrite(pin=pin, value=value))

    try:
        asyncio.run(_run())
    except (OSError, RuntimeError, ValueError) as e:
        sys.stderr.write(f"Error: local gRPC IPC DigitalWrite failed: {e}\n")
        sys.exit(4)


@app.command()
def main(
    state: Annotated[str, typer.Argument(help="State to set (on/off)")],
    pin: Annotated[int, typer.Argument(help="Pin number")] = 13,
) -> None:
    """Set the MCU pin state via local gRPC bridge."""
    state_norm = state.lower()
    if state_norm not in ("on", "off"):
        sys.stderr.write(f"Error: invalid state '{state}'. Use on|off.\n")
        sys.exit(2)

    val = 1 if state_norm == "on" else 0
    set_led_state(pin, val)


if __name__ == "__main__":
    app()
