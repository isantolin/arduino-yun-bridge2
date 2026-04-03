#!/usr/bin/env python3
"""CLI utility to push files to the MCU Bridge (Linux or MCU storage)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiomqtt
import click
from mcubridge.config.const import DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT
from mcubridge.config.settings import get_uci_config

@click.command()
@click.argument("source_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("target_path")
@click.option("--mcu", is_flag=True, help="Target MCU storage (e.g. SD card)")
@click.option("--host", default=None, help="MQTT host")
@click.option("--port", default=None, type=int, help="MQTT port")
def main(
    source_file: str,
    target_path: str,
    mcu: bool,
    host: str | None,
    port: int | None,
) -> None:
    """Push SOURCE_FILE to TARGET_PATH on the bridge."""

    config = get_uci_config()
    resolved_host: str = host or str(config.get("mqtt_host") or DEFAULT_MQTT_HOST)
    resolved_port: int = port or int(config.get("mqtt_port") or DEFAULT_MQTT_PORT)
    prefix: str = str(config.get("mqtt_topic") or "br")

    if mcu:
        topic = f"{prefix}/file/write/mcu/{target_path.lstrip('/')}"
    else:
        topic = f"{prefix}/file/write/{target_path.lstrip('/')}"

    async def push() -> None:
        try:
            async with aiomqtt.Client(resolved_host, resolved_port) as client:
                data = Path(source_file).read_bytes()
                click.echo(f"Pushing {len(data)} bytes to {topic}...")
                await client.publish(topic, payload=data, qos=1)
                click.echo("Success.")
        except (aiomqtt.MqttError, OSError, ValueError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    asyncio.run(push())

if __name__ == "__main__":
    main()
