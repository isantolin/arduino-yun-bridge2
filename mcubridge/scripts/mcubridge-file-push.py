#!/usr/bin/env python3
"""Modernized File Push utility for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import aiomqtt
import typer
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol.topics import Topic, topic_path

app = typer.Typer(add_completion=False, help="Push files to MCU or Linux storage.")


async def push_file(topic: str, data: bytes) -> None:
    """Publish file data using core configuration."""
    config = load_runtime_config()
    tls_context = config.get_ssl_context()

    try:
        async with aiomqtt.Client(
            hostname=config.mqtt_host,
            port=config.mqtt_port,
            username=config.mqtt_user or None,
            password=config.mqtt_pass or None,
            tls_context=tls_context,
        ) as client:
            await client.publish(topic, payload=data, qos=1)
    except (aiomqtt.MqttError, OSError, RuntimeError) as e:
        sys.stderr.write(f"Error: File push failed: {e}\n")
        raise typer.Exit(code=1)


@app.command()
def main(
    source: Annotated[Path, typer.Argument(help="Source file to push", exists=True, dir_okay=False)],
    target: Annotated[str, typer.Argument(help="Target path on the bridge")],
    mcu: Annotated[bool, typer.Option(help="Target MCU storage")] = False,
) -> None:
    """Push file data to the bridge via MQTT."""
    config = load_runtime_config()
    prefix = config.mqtt_topic

    clean_target = target.lstrip("/")

    segments = ["write"]
    if mcu:
        segments.append("mcu")
    segments.append(clean_target)

    topic = topic_path(prefix, Topic.FILE, *segments)

    data = source.read_bytes()
    typer.echo(f"Pushing {len(data)} bytes to {topic}...")

    asyncio.run(push_file(topic, data))
    typer.echo("Success.")


if __name__ == "__main__":
    app()
