#!/usr/bin/env python3
"""Modernized LED control script for MCU Bridge (SIL-2)."""

from __future__ import annotations

import socket
import sys
from mcubridge.config.settings import load_runtime_config
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.topics import Topic, topic_path


def do_publish(topic: str, payload: str) -> None:
    """Publish LED state using direct UNIX socket IPC."""
    msg = pb.MqttQueuedPublish(
        topic_name=topic,
        payload=payload.encode("utf-8"),
        qos=1,
    )
    payload_data = msg.SerializeToString()
    prefix = len(payload_data).to_bytes(4, byteorder="big")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect("/var/run/mcubridge.sock")
        sock.sendall(prefix + payload_data)
    except OSError as e:
        sys.stderr.write(f"Error: UNIX socket publication failed: {e}\n")
        sys.exit(4)
    finally:
        sock.close()


def main() -> None:
    """Set the MCU pin state via MQTT bridge."""
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Control MCU LED via MQTT.")
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
