#!/usr/bin/env python3
"""Modernized File Push utility for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
import argparse
from pathlib import Path
import aiomqtt
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol.topics import Topic, topic_path


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
        sys.exit(1)


def main() -> None:
    """Push file data to the bridge via MQTT."""
    parser = argparse.ArgumentParser(description="Push files to MCU or Linux storage.")
    parser.add_argument("source", type=Path, help="Source file to push")
    parser.add_argument("target", help="Target path on the bridge")
    parser.add_argument("--mcu", action="store_true", help="Target MCU storage")
    args = parser.parse_args()

    if not args.source.exists() or args.source.is_dir():
        sys.stderr.write(
            f"Error: source file '{args.source}' does not exist or is a directory.\n"
        )
        sys.exit(2)

    config = load_runtime_config()
    prefix = config.mqtt_topic

    clean_target = args.target.lstrip("/")

    segments = ["write"]
    if args.mcu:
        segments.append("mcu")
    segments.append(clean_target)

    topic = topic_path(prefix, Topic.FILE, *segments)

    data = args.source.read_bytes()
    print(f"Pushing {len(data)} bytes to {topic}...")

    asyncio.run(push_file(topic, data))
    print("Success.")


if __name__ == "__main__":
    main()
