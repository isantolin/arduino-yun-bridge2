#!/usr/bin/env python3
"""Modernized LED control script for MCU Bridge (SIL-2)."""

from __future__ import annotations

import asyncio
import sys
import argparse
import aiomqtt
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol.topics import Topic, topic_path


async def do_publish(topic: str, payload: str) -> None:
    """Publish LED state using core configuration."""
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
            await client.publish(topic, payload=payload, qos=1)
    except (aiomqtt.MqttError, OSError, RuntimeError) as e:
        sys.stderr.write(f"Error: MQTT publication failed: {e}\n")
        sys.exit(4)


def main() -> None:
    """Set the MCU pin state via MQTT bridge."""
    parser = argparse.ArgumentParser(description="Control MCU LED via MQTT.")
    parser.add_argument("state", help="State to set (on/off)")
    parser.add_argument("pin", type=int, nargs="?", default=13, help="Pin number")
    args = parser.parse_args()

    state_norm = args.state.lower()
    if state_norm not in ("on", "off"):
        sys.stderr.write(f"Error: invalid state '{args.state}'. Use on|off.\n")
        sys.exit(2)

    config = load_runtime_config()
    topic = topic_path(config.mqtt_topic, Topic.DIGITAL, args.pin)
    payload = "1" if state_norm == "on" else "0"

    asyncio.run(do_publish(topic, payload))


if __name__ == "__main__":
    main()
