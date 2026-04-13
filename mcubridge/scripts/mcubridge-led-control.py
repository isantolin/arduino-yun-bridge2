#!/usr/bin/env python3
"""Modernized LED control script for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
from typing import Annotated, cast

import aiomqtt
import typer
from mcubridge.config.settings import get_uci_config
from mcubridge.protocol.structures import RuntimeConfig
from mcubridge.util.mqtt_helper import configure_tls_context

app = typer.Typer(add_completion=False, help="Control MCU LED via MQTT.")


async def do_publish(topic: str, payload: str) -> None:
    """Publish LED state using core configuration."""
    config = cast(RuntimeConfig, get_uci_config())
    tls_context = configure_tls_context(config)

    try:
        async with aiomqtt.Client(
            hostname=config.mqtt_host,
            port=config.mqtt_port,
            username=config.mqtt_user or None,
            password=config.mqtt_pass or None,
            tls_context=tls_context,
        ) as client:
            await client.publish(topic, payload=payload, qos=1)
    except (aiomqtt.MqttError, OSError, RuntimeError) as e:
        sys.stderr.write(f"Error: MQTT publication failed: {e}\n")
        raise typer.Exit(code=4)


@app.command()
def main(
    state: Annotated[str, typer.Argument(help="State to set (on/off)")],
    pin: Annotated[int, typer.Argument(help="Pin number")] = 13,
) -> None:
    """Set the MCU pin state via MQTT bridge."""
    state_norm = state.lower()
    if state_norm not in ("on", "off"):
        sys.stderr.write(f"Error: invalid state '{state}'. Use on|off.\n")
        raise typer.Exit(code=2)

    config = cast(RuntimeConfig, get_uci_config())
    topic = f"{config.mqtt_topic}/d/{pin}"
    payload = "1" if state_norm == "on" else "0"

    asyncio.run(do_publish(topic, payload))


if __name__ == "__main__":
    app()
